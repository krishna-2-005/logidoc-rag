import { CheckCircle2, X, XCircle } from "lucide-react"
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react"
import { cn } from "@/lib/utils"

type ToastTone = "ok" | "bad"

interface ToastItem {
  id: number
  tone: ToastTone
  message: string
}

interface ToastApi {
  success: (message: string) => void
  error: (message: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

let counter = 0

/** Short confirmations and failures, announced politely to screen readers. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const dismiss = useCallback((id: number) => setItems((current) => current.filter((item) => item.id !== id)), [])

  const push = useCallback(
    (tone: ToastTone, message: string) => {
      const id = ++counter
      setItems((current) => [...current.slice(-3), { id, tone, message }])
      window.setTimeout(() => dismiss(id), tone === "bad" ? 9000 : 5000)
    },
    [dismiss],
  )

  const api = useMemo<ToastApi>(() => ({ success: (message) => push("ok", message), error: (message) => push("bad", message) }), [push])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2" aria-live="polite">
        {items.map((item) => (
          <div
            key={item.id}
            role={item.tone === "bad" ? "alert" : "status"}
            className={cn(
              "pointer-events-auto flex items-start gap-2 rounded-lg border bg-card p-3 text-sm shadow-lg",
              item.tone === "ok" ? "border-ok/30" : "border-bad/30",
            )}
          >
            {item.tone === "ok" ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" aria-hidden="true" /> : <XCircle className="mt-0.5 size-4 shrink-0 text-bad" aria-hidden="true" />}
            <p className="flex-1">{item.message}</p>
            <button type="button" aria-label="Dismiss" className="text-muted-foreground hover:text-foreground" onClick={() => dismiss(item.id)}>
              <X className="size-4" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext)
  if (!context) throw new Error("useToast needs a ToastProvider")
  return context
}
