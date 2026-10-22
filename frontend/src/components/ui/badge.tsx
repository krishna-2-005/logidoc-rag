import { cva, type VariantProps } from "class-variance-authority"
import type { HTMLAttributes } from "react"
import { cn } from "@/lib/utils"

const badgeVariants = cva("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium [&_svg]:size-3", {
  variants: {
    tone: {
      neutral: "border-border bg-secondary text-secondary-foreground",
      ok: "border-ok/20 bg-ok-soft text-ok",
      warn: "border-warn/25 bg-warn-soft text-warn",
      bad: "border-bad/20 bg-bad-soft text-bad",
      info: "border-info/20 bg-info-soft text-info",
    },
  },
  defaultVariants: { tone: "neutral" },
})

export type Tone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />
}
