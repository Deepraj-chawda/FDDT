import * as React from "react"

import { AuditDetails } from "@/components/AuditDetails"
import { AuditRow } from "@/design-system/AuditRow"
import { auditLabel, describeAuditEvent } from "@/lib/audit"
import type { AuditLogEntry } from "@/types/case"

// Full-width horizontal audit-trail strip — restyled against
// stitch_docauth_document_review_platform/docauth_case_detail's own
// "Audit trail" section (a bottom-of-page 3-column AuditRow grid, most-
// recent first), replacing this component's earlier vertical-sidebar
// layout. Still reads the same real audit_log rows (backend/app/api/
// cases.py's GET .../audit-log) — every entry here already happened,
// nothing is synthesized for display.

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
}

function eventDetail(entry: AuditLogEntry): string | null {
  return describeAuditEvent(entry.event_type, entry.event_data)
}

export function ActivityTimeline({
  entries,
  isLoading,
  showDetails = false,
}: {
  entries: AuditLogEntry[]
  isLoading: boolean
  // Platform admins: every recorded field of every event (the API also
  // gives them the platform-only events — deletes, restores, support views).
  showDetails?: boolean
}) {
  // Platform admins also receive a record of every support view of the case
  // (one per page load); they bury the events that matter, so they are
  // folded away behind a toggle — still all there.
  const [showAccess, setShowAccess] = React.useState(false)
  const accessCount = entries.filter((e) => e.event_type === "platform_admin_access").length
  const visible = showAccess ? entries : entries.filter((e) => e.event_type !== "platform_admin_access")
  // Newest first, matching the reference's own ordering — the API
  // returns oldest-first (append-only insert order), so this just
  // reverses for display without touching the fetch.
  const newestFirst = [...visible].reverse()

  return (
    <section className="w-full rounded-lg border border-border bg-card p-4 shadow-card">
      <div className="mb-3 flex items-center gap-2 border-b border-border pb-3">
        <h3 className="text-xs font-bold uppercase tracking-wide text-foreground">Audit trail</h3>
        {accessCount > 0 && (
          <label className="ml-auto inline-flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground">
            <input type="checkbox" checked={showAccess} onChange={(e) => setShowAccess(e.target.checked)} />
            Show {accessCount} support view{accessCount === 1 ? "" : "s"}
          </label>
        )}
      </div>
      {isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
      {!isLoading && newestFirst.length === 0 && (
        <p className="text-xs text-muted-foreground">Nothing logged yet.</p>
      )}
      {!isLoading && newestFirst.length > 0 && (
        <div className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
          {newestFirst.map((entry, index) => (
            <AuditRow
              key={entry.id}
              current={index === 0}
              timestamp={formatTimestamp(entry.created_at)}
              title={auditLabel(entry.event_type)}
              description={
                [eventDetail(entry), entry.actor_name].filter(Boolean).join(" · ") || "Automated system event"
              }
            >
              {showDetails && <AuditDetails data={entry.event_data} />}
            </AuditRow>
          ))}
        </div>
      )}
    </section>
  )
}
