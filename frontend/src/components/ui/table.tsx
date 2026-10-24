import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react"
import { cn } from "@/lib/utils"

export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto rounded-lg border bg-card shadow-card" tabIndex={0} role="region" aria-label="Scrollable table">
      <table className={cn("w-full caption-bottom text-sm", className)} {...props} />
    </div>
  )
}

export const TableHeader = (props: HTMLAttributes<HTMLTableSectionElement>) => <thead className="bg-muted/70 [&_tr]:border-b" {...props} />
export const TableBody = (props: HTMLAttributes<HTMLTableSectionElement>) => <tbody className="[&_tr:last-child]:border-0" {...props} />

export const TableRow = ({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) => (
  <tr className={cn("border-b transition-colors hover:bg-muted/40 data-[selected=true]:bg-accent", className)} {...props} />
)

export const TableHead = ({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) => (
  <th scope="col" className={cn("h-10 whitespace-nowrap px-4 text-left align-middle text-xs font-medium text-muted-foreground", className)} {...props} />
)

export const TableCell = ({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) => (
  <td className={cn("px-4 py-3 align-middle", className)} {...props} />
)
