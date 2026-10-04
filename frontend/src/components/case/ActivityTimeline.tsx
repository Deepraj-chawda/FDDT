import { AuditRow } from "@/design-system/AuditRow"
import { describeAuditEvent } from "@/lib/audit"
import { AUDIT_EVENT_LABELS, type AuditLogEntry } from "@/types/case"

// Full-width horizontal audit-trail strip — restyled against
// stitch_docauth_document_review_platform/docauth_case_detail's own
// "Audit trail" section (a bottom-of-page 3-column AuditRow grid, most-
// recent first), replacing this component's earlier vertical-sidebar
// layout. Still reads the same real audit_log rows (backend/app/api/
// cases.py's GET .../audit-log) — every entry here already happened,
// nothing is synthesized for display.

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
}

function eventDetail(entry: AuditLogEntry): string | null {
  return describeAuditEvent(entry.event_type, entry.event_data)
}

export function ActivityTimeline({
  entries,
  isLoading,
}: {
  entries: AuditLogEntry[]
  isLoading: boolean
}) {
  // Newest first, matching the reference's own ordering — the API
  // returns oldest-first (append-only insert order), so this just
  // reverses for display without touching the fetch.
  const newestFirst = [...entries].reverse()

  return (
    <section className="w-full rounded-lg border border-border bg-card p-4 shadow-card">
      <div className="mb-3 flex items-center gap-2 border-b border-border pb-3">
        <h3 className="text-xs font-bold uppercase tracking-wide text-foreground">Audit trail</h3>
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
              title={AUDIT_EVENT_LABELS[entry.event_type] ?? entry.event_type}
              description={
                [eventDetail(entry), entry.actor_name].filter(Boolean).join(" · ") || "Automated system event"
              }
            />
          ))}
        </div>
      )}
    </section>
  )
}
