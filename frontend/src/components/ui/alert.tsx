import { cva, type VariantProps } from "class-variance-authority"
import type { HTMLAttributes } from "react"
import { cn } from "@/lib/utils"

const alertVariants = cva("flex gap-3 rounded-lg border p-3.5 text-sm [&_svg]:mt-0.5 [&_svg]:size-4 [&_svg]:shrink-0", {
  variants: {
    tone: {
      info: "border-info/20 bg-info-soft text-info",
      ok: "border-ok/20 bg-ok-soft text-ok",
      warn: "border-warn/25 bg-warn-soft text-warn",
      bad: "border-bad/20 bg-bad-soft text-bad",
    },
  },
  defaultVariants: { tone: "info" },
})

export function Alert({ className, tone, role, ...props }: HTMLAttributes<HTMLDivElement> & VariantProps<typeof alertVariants>) {
  return <div role={role ?? (tone === "bad" ? "alert" : "status")} className={cn(alertVariants({ tone }), className)} {...props} />
}
