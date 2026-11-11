import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** Labelled values: an empty value says "Not recorded" rather than leaving a gap or inventing one. */
export function Facts({ items, columns = 2, label }: { items: [string, ReactNode][]; columns?: 1 | 2 | 3 | 4; label?: string }) {
  return (
    <dl aria-label={label} className={cn("grid gap-x-6 gap-y-4 text-sm", { 1: "", 2: "sm:grid-cols-2", 3: "sm:grid-cols-2 lg:grid-cols-3", 4: "sm:grid-cols-2 lg:grid-cols-4" }[columns])}>
      {items.map(([name, value]) => (
        <div key={name} className="min-w-0">
          <dt className="text-xs font-medium text-muted-foreground">{name}</dt>
          <dd className="mt-1 break-words">{value === null || value === undefined || value === "" ? <span className="text-muted-foreground">Not recorded</span> : value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** A JSON value as an operator reads it (a payload preview, a cursor) */
export function JsonBlock({ value, label }: { value: unknown; label: string }) {
  return (
    <pre aria-label={label} tabIndex={0} className="max-h-96 overflow-auto rounded-md border bg-muted/50 p-3 font-mono text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}
