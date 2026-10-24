import { useId, useRef, type KeyboardEvent, type ReactNode } from "react"
import { cn } from "@/lib/utils"

export interface TabItem {
  id: string
  label: string
  badge?: ReactNode
}

/**
 * Accessible tabs: a tablist with roving focus (arrow keys, Home, End) and a
 * panel for the selected tab. The caller renders the content of the active tab.
 */
export function Tabs({ items, value, onChange, children, label }: { items: TabItem[]; value: string; onChange: (id: string) => void; children: ReactNode; label: string }) {
  const base = useId()
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})

  const move = (event: KeyboardEvent, index: number) => {
    const last = items.length - 1
    const target = event.key === "ArrowRight" ? (index + 1) % items.length : event.key === "ArrowLeft" ? (index - 1 + items.length) % items.length : event.key === "Home" ? 0 : event.key === "End" ? last : null

    if (target === null) return

    event.preventDefault()
    onChange(items[target].id)
    refs.current[items[target].id]?.focus()
  }

  return (
    <div>
      <div role="tablist" aria-label={label} className="mb-6 flex gap-1 overflow-x-auto overflow-y-hidden border-b [scrollbar-width:none]">
        {items.map((item, index) => {
          const selected = item.id === value

          return (
            <button
              key={item.id}
              ref={(node) => {
                refs.current[item.id] = node
              }}
              role="tab"
              type="button"
              id={`${base}-tab-${item.id}`}
              aria-selected={selected}
              aria-controls={`${base}-panel`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(item.id)}
              onKeyDown={(event) => move(event, index)}
              className={cn(
                "-mb-px flex items-center gap-2 whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
                selected ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {item.label}
              {item.badge}
            </button>
          )
        })}
      </div>
      <div role="tabpanel" id={`${base}-panel`} aria-labelledby={`${base}-tab-${value}`} tabIndex={0}>
        {children}
      </div>
    </div>
  )
}
