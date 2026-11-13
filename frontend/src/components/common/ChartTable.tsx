import type { ReactNode } from "react"

/**
 * The numbers behind a chart, in a table the reader can open. Every chart that
 * uses a colour below 3:1 against the surface carries one, so no value depends on
 * telling colours apart.
 */
export function ChartTable({ caption, columns, rows }: { caption: string; columns: string[]; rows: ReactNode[][] }) {
  return (
    <details className="mt-3 text-xs">
      <summary className="cursor-pointer font-medium text-primary">Show the numbers</summary>
      <div className="mt-2 max-h-64 overflow-auto rounded-md border">
        <table className="w-full text-left">
          <caption className="sr-only">{caption}</caption>
          <thead className="sticky top-0 bg-muted">
            <tr>
              {columns.map((column, index) => (
                <th key={column} scope="col" className={`px-2 py-1.5 font-medium text-muted-foreground ${index ? "text-right" : ""}`}>
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index} className="border-t">
                {row.map((cell, column) => (
                  <td key={column} className={`tabular px-2 py-1 ${column ? "text-right" : ""}`}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
}

/** "12 Nov" from "2026-11-12" (UTC days, as the server counts them) */
export function shortDay(day: string): string {
  const moment = new Date(`${day}T00:00:00Z`)

  return Number.isNaN(moment.getTime()) ? day : moment.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })
}
