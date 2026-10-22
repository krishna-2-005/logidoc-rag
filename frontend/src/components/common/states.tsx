import { AlertCircle, Inbox, RefreshCw } from "lucide-react"
import type { ReactNode } from "react"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api"

/** A region that tells assistive technology it is loading, with shapes where the content will be. */
export function LoadingBlock({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="space-y-3">
      <span className="sr-only">{label}…</span>
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-10 w-full" />
      ))}
    </div>
  )
}

export function KpiSkeletons({ count = 4 }: { count?: number }) {
  return (
    <div role="status" aria-busy="true" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <span className="sr-only">Loading figures…</span>
      {Array.from({ length: count }, (_, index) => (
        <Card key={index} className="p-5">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="mt-4 h-8 w-20" />
          <Skeleton className="mt-3 h-3 w-32" />
        </Card>
      ))}
    </div>
  )
}

/** A meaningful empty state: what is missing and what to do about it. */
export function EmptyState({ title, children, action, icon }: { title: string; children?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed bg-card/60 px-6 py-12 text-center">
      <div className="mb-3 rounded-full bg-secondary p-3 text-muted-foreground" aria-hidden="true">
        {icon ?? <Inbox className="size-5" />}
      </div>
      <h3 className="text-sm font-semibold">{title}</h3>
      {children && <div className="mt-1 max-w-md text-sm text-muted-foreground">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "Something went wrong."
}

export function ErrorState({ error, onRetry, title = "This could not be loaded" }: { error: unknown; onRetry?: () => void; title?: string }) {
  return (
    <Alert tone="bad" className="items-start">
      <AlertCircle />
      <div className="flex-1">
        <p className="font-medium">{title}</p>
        <p className="mt-0.5 text-sm opacity-90">{errorMessage(error)}</p>
        {onRetry && (
          <Button variant="outline" size="sm" className="mt-3 bg-card text-foreground" onClick={onRetry}>
            <RefreshCw /> Try again
          </Button>
        )}
      </div>
    </Alert>
  )
}
