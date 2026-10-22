import { ArrowRight } from "lucide-react"
import { StatusBadge } from "@/components/common/StatusBadge"
import { dateTime, NA } from "@/lib/format"
import type { SnapshotChanges, TimelineEntry } from "@/lib/types"

const GROUPS: [keyof SnapshotChanges, string][] = [
  ["new", "New"],
  ["reappeared", "Reappeared"],
  ["changed", "Changed"],
  ["resolved", "No longer detected"],
  ["unverified", "Not verified now (not treated as resolved)"],
  ["persistent", "Unchanged"],
]

export function hasChanges(changes: SnapshotChanges): boolean {
  return !!changes.status_change || changes.input_changed || changes.value_changes.length > 0 || GROUPS.some(([key]) => key !== "persistent" && (changes[key] as unknown[]).length > 0)
}

/** What changed between two recorded audits of one shipment: the previous value against the current one. */
export function ChangeSummary({ changes }: { changes: SnapshotChanges }) {
  if (changes.first) {
    return <p className="text-xs text-muted-foreground">First recorded audit of this shipment.</p>
  }

  return (
    <div className="space-y-2 text-sm">
      {changes.status_change && (
        <p className="flex flex-wrap items-center gap-2">
          <span className="text-muted-foreground">Result</span>
          <StatusBadge status={changes.status_change[0]} />
          <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden="true" />
          <StatusBadge status={changes.status_change[1]} />
        </p>
      )}

      {changes.input_changed && <p className="text-xs text-muted-foreground">The shipment's documents changed between these audits.</p>}

      {GROUPS.map(([key, title]) => {
        const items = changes[key] as { finding_key: string; category: string; summary: string }[]
        if (!items.length) return null

        return (
          <div key={key}>
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5">
              {items.map((item) => (
                <li key={item.finding_key}>
                  <span className="font-medium">{item.category}</span>: {item.summary}
                </li>
              ))}
            </ul>
          </div>
        )
      })}

      {changes.value_changes.length > 0 && (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 pr-3 font-medium">Value</th>
              <th className="py-1 pr-3 font-medium">Previous</th>
              <th className="py-1 font-medium">Now</th>
            </tr>
          </thead>
          <tbody>
            {changes.value_changes.map((item) => (
              <tr key={item.field} className="border-t">
                <td className="py-1 pr-3">{item.label}</td>
                <td className="tabular py-1 pr-3">{item.previous === null ? NA : String(item.previous)}</td>
                <td className="tabular py-1 font-medium">{item.current === null ? NA : String(item.current)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!hasChanges(changes) && <p className="text-xs text-muted-foreground">Nothing changed since the previous audit.</p>}
    </div>
  )
}

export function Timeline({ entries }: { entries: TimelineEntry[] }) {
  return (
    <ol className="space-y-4">
      {entries.map(({ snapshot, changes }) => (
        <li key={snapshot.id} className="relative rounded-lg border bg-card p-4 shadow-card">
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="text-sm font-semibold">{dateTime(snapshot.created_at)}</span>
            <StatusBadge status={snapshot.final_status ?? "NOT RECONCILED"} />
            <span className="text-xs text-muted-foreground">
              {snapshot.discrepancy_count} discrepanc{snapshot.discrepancy_count === 1 ? "y" : "ies"} · {snapshot.review_count} to review · snapshot #{snapshot.id}
              {snapshot.runs.length > 1 ? ` · found unchanged by ${snapshot.runs.length - 1} later audit${snapshot.runs.length === 2 ? "" : "s"}` : ""}
            </span>
          </div>
          <ChangeSummary changes={changes} />
        </li>
      ))}
    </ol>
  )
}
