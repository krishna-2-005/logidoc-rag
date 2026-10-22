import type { HTMLAttributes } from "react"
import { cn } from "@/lib/utils"

/** A placeholder while data loads. aria-hidden: the surrounding region announces the loading state. */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden="true" className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />
}
