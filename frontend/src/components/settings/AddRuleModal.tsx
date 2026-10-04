import * as React from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Loader2Icon } from "lucide-react"

import { ApiError } from "@/api/client"
import { createRiskRule, getRiskRuleOptions } from "@/api/settings"
import { Modal } from "@/components/settings/Modal"
import { StatusLine } from "@/components/settings/SettingsShell"
import { FieldError, invalidFieldClass } from "@/components/ui/field-error"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import type { RiskRuleCreate, RuleMatchKind, RuleSeverity } from "@/types/settings"

const SEVERITIES: RuleSeverity[] = ["low", "medium", "high"]
const inputClass =
  "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
const RULE_ID_PATTERN = /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/

function Field({
  label,
  htmlFor,
  hint,
  error,
  children,
}: {
  label: string
  htmlFor?: string
  hint?: string
  error?: string | null
  children: React.ReactNode
}) {
  return (
    <div>
      <label htmlFor={htmlFor} className="mb-1 block text-xs font-semibold text-slate-700">
        {label}
      </label>
      {children}
      <FieldError id={htmlFor ? `${htmlFor}-error` : undefined} message={error} />
      {hint && <p className="mt-1 text-[11px] text-slate-400">{hint}</p>}
    </div>
  )
}

// "Add rule": the admin picks WHAT the rule reacts to from what the checks can
// actually produce (served by the backend, so a name that could never match
// isn't offered), then how much it weighs. No JSON is written by hand. The rule
// starts at version 1 and applies to cases scored from now on — cases already
// scored keep the assessment (and rule versions) they were scored with.
export function AddRuleModal({
  onClose,
  onCreated,
  submit,
}: {
  onClose: () => void
  onCreated: (rule: { rule_id: string }) => void
  /** Where to save the rule. Defaults to the acting company's own risk rules;
   * Platform › Rule templates passes the template endpoint instead. */
  submit?: (payload: RiskRuleCreate) => Promise<{ rule_id: string }>
}) {
  const { token } = useAuth()
  const { platformCompanyParam: cid } = useActingCompany()
  const { data: options, isLoading, isError } = useQuery({
    queryKey: ["riskRuleOptions", token],
    queryFn: () => getRiskRuleOptions(token as string),
    enabled: Boolean(token),
  })

  const [ruleId, setRuleId] = React.useState("custom.")
  const [category, setCategory] = React.useState("forensics")
  const [match, setMatch] = React.useState<RuleMatchKind>("finding")
  const [checkType, setCheckType] = React.useState("")
  const [finding, setFinding] = React.useState("")
  const [severityIn, setSeverityIn] = React.useState<RuleSeverity[]>([])
  const [subCheck, setSubCheck] = React.useState("")
  const [fieldName, setFieldName] = React.useState("")
  const [signatureResult, setSignatureResult] = React.useState("")
  const [weight, setWeight] = React.useState("10")
  const [severity, setSeverity] = React.useState<RuleSeverity>("medium")
  const [reason, setReason] = React.useState("")
  const [active, setActive] = React.useState(true)
  const [note, setNote] = React.useState("")
  const [error, setError] = React.useState<string | null>(null)

  // Changing the rule type clears parameters that belonged to the old one.
  const changeMatch = (next: RuleMatchKind) => {
    setMatch(next)
    setCheckType("")
    setFinding("")
    setSubCheck("")
    setFieldName("")
    setSignatureResult("")
    setSeverityIn([])
  }

  const create = useMutation({
    mutationFn: () => {
      const payload: RiskRuleCreate = {
        rule_id: ruleId.trim(),
        category,
        match,
        weight: Number(weight),
        severity,
        reason_template: reason.trim(),
        is_active: active,
        change_note: note.trim() || null,
        ...(match === "finding" ? { check_type: checkType, finding, severity_in: severityIn } : {}),
        ...(match === "check_result" ? { check_type: checkType } : {}),
        ...(match === "sub_check" ? { sub_check: subCheck } : {}),
        ...(match === "cross_document" ? { field_name: fieldName, severity_in: severityIn } : {}),
        ...(match === "signature_match" ? { signature_result: signatureResult } : {}),
      }
      return submit ? submit(payload) : createRiskRule(payload, token as string, cid)
    },
    onSuccess: onCreated,
    onError: (err) => setError(err instanceof ApiError ? err.message : "Couldn't create the rule. Please try again."),
  })

  const findingsForCheck = options?.finding_checks.find((c) => c.value === checkType)?.findings ?? []
  const w = Number(weight)
  const parametersOk =
    (match === "finding" && checkType !== "" && finding !== "") ||
    (match === "check_result" && checkType !== "") ||
    (match === "sub_check" && subCheck !== "") ||
    (match === "cross_document" && fieldName !== "") ||
    (match === "signature_match" && signatureResult !== "")
  // Each problem is shown in red under its own field once Create rule was clicked.
  const [attempted, setAttempted] = React.useState(false)
  const fieldErrors = {
    ruleId: RULE_ID_PATTERN.test(ruleId.trim())
      ? null
      : "Name the rule with lower-case words and one dot, e.g. custom.hidden_layers.",
    parameters: parametersOk ? null : "Choose what the rule should react to.",
    weight:
      weight.trim() === "" || !Number.isFinite(w) || w < -100 || w > 100
        ? "The weight must be a number from -100 to 100."
        : null,
    reason: reason.trim().length < 3 ? "Write the reason reviewers will read when this rule fires (at least 3 characters)." : null,
  }
  const problem = Object.values(fieldErrors).some(Boolean)
  const shown = (field: keyof typeof fieldErrors) => (attempted ? fieldErrors[field] : null)
  const withError = (base: string, field: keyof typeof fieldErrors) => (shown(field) ? `${base} ${invalidFieldClass}` : base)

  const toggleSeverity = (s: RuleSeverity) =>
    setSeverityIn((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]))

  const severityFilter = (
    <Field label="Only when the finding's severity is" hint="Leave all unchecked to match any severity.">
      <div className="flex gap-4">
        {SEVERITIES.map((s) => (
          <label key={s} className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-semibold capitalize text-slate-700">
            <input type="checkbox" className="size-4 accent-blue-600" checked={severityIn.includes(s)} onChange={() => toggleSeverity(s)} />
            {s}
          </label>
        ))}
      </div>
    </Field>
  )

  return (
    <Modal
      wide
      title="Add risk rule"
      description="A rule adds its weight to a case's score whenever its condition is met. It applies to cases scored from now on — cases already scored are never re-scored."
      onClose={onClose}
      busy={create.isPending}
    >
      {isLoading ? (
        <p className="text-sm text-slate-400">Loading options…</p>
      ) : isError || !options ? (
        <p className="text-sm text-rose-600">Couldn't load the rule options.</p>
      ) : (
        <form
          className="flex flex-col gap-4"
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            setError(null)
            setAttempted(true)
            if (problem) return
            create.mutate()
          }}
        >
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Rule name" htmlFor="rule-id" error={shown("ruleId")} hint="A stable key: lower-case words, one dot.">
              <input id="rule-id" aria-invalid={Boolean(shown("ruleId"))} aria-describedby="rule-id-error" className={withError(`${inputClass} font-mono`, "ruleId")} value={ruleId} onChange={(e) => setRuleId(e.target.value)} autoFocus />
            </Field>
            <Field label="Category" htmlFor="rule-category">
              <select id="rule-category" className={inputClass} value={category} onChange={(e) => setCategory(e.target.value)}>
                {options.categories.map((c) => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </select>
            </Field>
          </div>

          <div className="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
            <div className="mb-3 text-[11px] font-bold uppercase tracking-wider text-slate-400">When this happens</div>
            <div className="flex flex-col gap-3">
              <Field label="Rule type" htmlFor="rule-match" error={shown("parameters")} hint={options.match_kinds.find((m) => m.value === match)?.help}>
                <select id="rule-match" className={inputClass} value={match} onChange={(e) => changeMatch(e.target.value as RuleMatchKind)}>
                  {options.match_kinds.map((m) => (
                    <option key={m.value} value={m.value}>{m.label}</option>
                  ))}
                </select>
              </Field>

              {match === "finding" && (
                <>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="Check" htmlFor="rule-check">
                      <select id="rule-check" className={inputClass} value={checkType} onChange={(e) => { setCheckType(e.target.value); setFinding("") }}>
                        <option value="">Choose a check…</option>
                        {options.finding_checks.map((c) => (
                          <option key={c.value} value={c.value}>{c.label}</option>
                        ))}
                      </select>
                    </Field>
                    <Field label="Finding" htmlFor="rule-finding">
                      <select id="rule-finding" className={`${inputClass} font-mono text-xs`} value={finding} disabled={!checkType} onChange={(e) => setFinding(e.target.value)}>
                        <option value="">{checkType ? "Choose a finding…" : "Choose a check first"}</option>
                        {findingsForCheck.map((f) => (
                          <option key={f} value={f}>{f}</option>
                        ))}
                      </select>
                    </Field>
                  </div>
                  {severityFilter}
                </>
              )}

              {match === "check_result" && (
                <Field label="Check flagged" htmlFor="rule-check-result">
                  <select id="rule-check-result" className={inputClass} value={checkType} onChange={(e) => setCheckType(e.target.value)}>
                    <option value="">Choose a check…</option>
                    {options.check_result_checks.map((c) => (
                      <option key={c.value} value={c.value}>{c.label}</option>
                    ))}
                  </select>
                </Field>
              )}

              {match === "sub_check" && (
                <Field label="Field-validation rule that failed" htmlFor="rule-sub-check">
                  <select id="rule-sub-check" className={inputClass} value={subCheck} onChange={(e) => setSubCheck(e.target.value)}>
                    <option value="">Choose a rule…</option>
                    {options.sub_checks.map((c) => (
                      <option key={c.value} value={c.value}>{c.label}</option>
                    ))}
                  </select>
                </Field>
              )}

              {match === "cross_document" && (
                <>
                  <Field label="Field the documents disagree on" htmlFor="rule-cross-field">
                    <select id="rule-cross-field" className={inputClass} value={fieldName} onChange={(e) => setFieldName(e.target.value)}>
                      <option value="">Choose a field…</option>
                      {options.cross_fields.map((c) => (
                        <option key={c.value} value={c.value}>{c.label}</option>
                      ))}
                    </select>
                  </Field>
                  {severityFilter}
                </>
              )}

              {match === "signature_match" && (
                <Field label="Signature comparison verdict" htmlFor="rule-sig" hint="An advisory visual comparison, not an identity match.">
                  <select id="rule-sig" className={inputClass} value={signatureResult} onChange={(e) => setSignatureResult(e.target.value)}>
                    <option value="">Choose a verdict…</option>
                    {options.signature_results.map((c) => (
                      <option key={c.value} value={c.value}>{c.label}</option>
                    ))}
                  </select>
                </Field>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Weight" htmlFor="rule-weight" error={shown("weight")} hint="Points added to the score (-100 to 100).">
              <input id="rule-weight" type="number" step="0.5" min={-100} max={100} aria-invalid={Boolean(shown("weight"))} aria-describedby="rule-weight-error" className={withError(`${inputClass} font-mono`, "weight")} value={weight} onChange={(e) => setWeight(e.target.value)} />
            </Field>
            <Field label="Severity" htmlFor="rule-severity">
              <select id="rule-severity" className={inputClass} value={severity} onChange={(e) => setSeverity(e.target.value as RuleSeverity)}>
                {SEVERITIES.map((s) => (
                  <option key={s} value={s} className="capitalize">{s[0].toUpperCase() + s.slice(1)}</option>
                ))}
              </select>
            </Field>
            <Field label="Status">
              <label className="mt-1 inline-flex cursor-pointer items-center gap-2 text-xs font-semibold text-slate-700">
                <input type="checkbox" className="size-4 accent-blue-600" checked={active} onChange={(e) => setActive(e.target.checked)} />
                Active from now
              </label>
            </Field>
          </div>

          <Field
            label="Reason shown to reviewers"
            htmlFor="rule-reason"
            error={shown("reason")}
            hint={`You can use ${options.placeholders.map((p) => `{${p}}`).join(" ")} — they are filled in when the rule fires.`}
          >
            <textarea
              id="rule-reason"
              rows={3}
              maxLength={1024}
              aria-invalid={Boolean(shown("reason"))}
              aria-describedby="rule-reason-error"
              className={withError(inputClass, "reason")}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="'{document}' has hidden layers. {description}"
            />
          </Field>

          <Field label="Note (optional)" htmlFor="rule-note">
            <input id="rule-note" className={inputClass} maxLength={1024} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why this rule is being added" />
          </Field>

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
              Add rule
            </button>
          </div>
        </form>
      )}
    </Modal>
  )
}
