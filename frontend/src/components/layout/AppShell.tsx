import {
  BadgeCheck,
  Bot,
  ChevronRight,
  ClipboardCheck,
  FileCheck2,
  FileDown,
  FileSearch,
  History,
  LayoutDashboard,
  LogOut,
  Menu,
  PackageSearch,
  Receipt,
  Scale,
  Search,
  Settings as SettingsIcon,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react"
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react"
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Tooltip } from "@/components/ui/tooltip"
import { can, useLogout, useOverview, useReadiness } from "@/hooks/queries"
import { ApiError } from "@/lib/api"
import type { Overview, Session } from "@/lib/types"
import { cn } from "@/lib/utils"

type Counter = (overview: Overview) => { value: number; tone: "neutral" | "bad" | "warn" } | null

interface Item {
  to: string
  label: string
  icon: LucideIcon
  end?: boolean
  permission?: string
  /** A live count shown beside the label (from the overview the dashboard already loads) */
  count?: Counter
}

const NAV: { label: string; items: Item[] }[] = [
  {
    label: "Operations",
    items: [
      { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
      { to: "/shipments", label: "Shipment Explorer", icon: PackageSearch, count: (o) => ({ value: o.kpis.identified, tone: "neutral" }) },
      { to: "/documents", label: "Document Intelligence", icon: FileSearch },
      { to: "/assistant", label: "AI Assistant", icon: Bot },
    ],
  },
  {
    label: "Audit",
    items: [
      { to: "/freight-audit", label: "Freight Audit", icon: Scale },
      { to: "/invoices", label: "Invoice Register", icon: Receipt, count: (o) => (o.exposure.mismatches > 0 ? { value: o.exposure.mismatches, tone: "bad" } : null) },
      { to: "/exceptions", label: "Exception Center", icon: ClipboardCheck, count: (o) => (o.reviews.open > 0 ? { value: o.reviews.open, tone: "bad" } : null) },
      { to: "/completeness", label: "Document Completeness", icon: FileCheck2, count: (o) => (o.completeness.incomplete > 0 ? { value: o.completeness.incomplete, tone: "warn" } : null) },
      { to: "/corrections", label: "Correction Approvals", icon: BadgeCheck, permission: "extraction.approve" },
    ],
  },
  {
    label: "Insights",
    items: [
      { to: "/history", label: "Audit History", icon: History },
      { to: "/reports", label: "Reports", icon: FileDown },
    ],
  },
  { label: "System", items: [{ to: "/settings", label: "Settings", icon: SettingsIcon }] },
]

/** The section and screen of the current address, for the top bar */
function whereAmI(pathname: string): { group: string; page: string; detail?: string } {
  for (const group of NAV) {
    for (const item of group.items) {
      const exact = pathname === item.to
      const below = item.to !== "/" && pathname.startsWith(`${item.to}/`)
      if (exact || below) return { group: group.label, page: item.label, detail: below ? decodeURIComponent(pathname.slice(item.to.length + 1)) : undefined }
    }
  }
  return { group: "Operations", page: "Overview" }
}

function Brand() {
  return (
    <div className="flex items-center gap-3 px-5 pb-4 pt-5">
      <span aria-hidden="true" className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground shadow-[0_6px_16px_-6px_hsl(var(--primary)/0.8)] ring-1 ring-white/15">
        <svg viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
          <path d="M3 8.5 12 4l9 4.5v7L12 20l-9-4.5z" />
          <path d="M3 8.5 12 13l9-4.5M12 13v7" />
        </svg>
      </span>
      <div>
        <p className="text-[15px] font-semibold leading-none tracking-tight text-white">LogiDoc</p>
        <p className="mt-1.5 text-[11px] leading-none text-navy-text/70">Freight audit intelligence</p>
      </div>
    </div>
  )
}

const COUNT_TONE = {
  neutral: "bg-white/10 text-navy-text",
  bad: "bg-bad text-white",
  warn: "bg-warn-soft text-warn",
}

function Navigation({ session, onNavigate }: { session: Session; onNavigate?: () => void }) {
  const overview = useOverview()
  const data = overview.data && !overview.data.empty ? overview.data : null

  return (
    <nav aria-label="Main" className="flex-1 space-y-4 overflow-y-auto px-3 pb-3">
      {NAV.map((group) => {
        const items = group.items.filter((item) => !item.permission || can(session, item.permission))
        if (items.length === 0) return null

        return (
          <div key={group.label}>
            <p className="px-3 pb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.09em] text-navy-text/60">{group.label}</p>
            <div className="space-y-0.5">
              {items.map(({ to, label, icon: Icon, end, count }) => {
                const badge = data && count ? count(data) : null

                return (
                  <NavLink
                    key={to}
                    to={to}
                    end={end}
                    onClick={onNavigate}
                    className={({ isActive }) =>
                      cn(
                        "relative flex items-center gap-3 rounded-lg px-3 py-[7px] text-[13.5px] font-medium text-navy-text/80 transition-colors hover:bg-white/[0.06] hover:text-white",
                        isActive && "bg-white/[0.09] text-white before:absolute before:inset-y-2 before:-left-3 before:w-[3px] before:rounded-r-full before:bg-primary hover:bg-white/[0.09]",
                      )
                    }
                  >
                    <Icon className="size-[18px] shrink-0" aria-hidden="true" />
                    <span className="truncate">{label}</span>
                    {badge && (
                      <span aria-hidden="true" className={cn("tabular ml-auto min-w-5 rounded-full px-1.5 py-px text-center text-[11px] font-semibold", COUNT_TONE[badge.tone])}>
                        {badge.value}
                      </span>
                    )}
                  </NavLink>
                )
              })}
            </div>
          </div>
        )
      })}
    </nav>
  )
}

function initials(name: string | undefined): string {
  return (name ?? "?").split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((part) => part[0]!.toUpperCase()).join("") || "?"
}

function Account({ session }: { session: Session }) {
  const logout = useLogout()

  return (
    <div className="border-t border-navy-line p-3 text-xs">
      {session.mode === "local" ? (
        <div className="flex gap-2 rounded-lg border border-white/10 bg-white/[0.04] p-2 text-[11px] leading-snug text-navy-text">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-300" aria-hidden="true" />
          <p>Local development mode. Nobody signs in; actions are recorded as not authenticated.</p>
        </div>
      ) : (
        <div className="flex items-center gap-3">
          <span aria-hidden="true" className="grid size-8 shrink-0 place-items-center rounded-full bg-primary text-[12px] font-semibold text-primary-foreground">
            {initials(session.user?.username)}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate font-medium text-white">{session.user?.username}</p>
            <p className="truncate text-navy-text/70">{session.user?.label}</p>
          </div>
          <Button variant="ghost" size="icon" aria-label="Sign out" className="text-navy-text hover:bg-white/10 hover:text-white" onClick={() => logout.mutate()}>
            <LogOut />
          </Button>
        </div>
      )}
    </div>
  )
}

const CHECK_NAMES: Record<string, string> = {
  documents: "Document folder",
  data_writable: "Data folder",
  extraction_store: "Extraction store",
  model_service: "Language models",
}

/** Whether the server can do its work right now (GET /api/ready), refreshed every minute */
function SystemStatus() {
  const ready = useReadiness()

  let tone: "ok" | "warn" | "bad" | "neutral" = "neutral"
  let text = "Checking status"

  if (ready.data) {
    tone = ready.data.status === "ready" ? "ok" : ready.data.status === "degraded" ? "warn" : "bad"
    text = ready.data.status === "ready" ? "All systems ready" : ready.data.status === "degraded" ? "Running degraded" : "Not ready"
  } else if (ready.isError) {
    tone = ready.error instanceof ApiError && ready.error.status === 503 ? "bad" : "neutral"
    text = tone === "bad" ? "Not ready" : "Status unavailable"
  }

  const details = ready.data ? (
    <ul className="space-y-0.5">
      {Object.entries(ready.data.checks).map(([name, check]) => (
        <li key={name}>
          {check.ok ? "✓" : "✗"} {CHECK_NAMES[name] ?? name}
        </li>
      ))}
    </ul>
  ) : (
    "The server's readiness check (GET /api/ready)"
  )

  const dot = { ok: "bg-emerald-500", warn: "bg-amber-500", bad: "bg-red-500", neutral: "bg-slate-400" }[tone]

  return (
    <Tooltip content={details} side="bottom">
      <button type="button" className="hidden shrink-0 items-center gap-2 whitespace-nowrap rounded-full border bg-card px-3 py-1.5 text-[12.5px] font-medium text-foreground shadow-card sm:flex">
        <span className="relative flex size-2">
          {tone === "ok" && <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />}
          <span className={cn("relative inline-flex size-2 rounded-full", dot)} />
        </span>
        {text}
      </button>
    </Tooltip>
  )
}

/** Jump to a shipment from anywhere: "/" focuses the box, Enter opens the filtered list */
function ShipmentSearch() {
  const navigate = useNavigate()
  const [query, setQuery] = useState("")
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const focus = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)
      if (event.key === "/" && !typing) {
        event.preventDefault()
        input.current?.focus()
      }
    }
    window.addEventListener("keydown", focus)
    return () => window.removeEventListener("keydown", focus)
  }, [])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const text = query.trim()
    navigate(text ? `/shipments?search=${encodeURIComponent(text)}` : "/shipments")
    setQuery("")
    input.current?.blur()
  }

  return (
    <form role="search" onSubmit={submit} className="relative hidden w-full max-w-sm md:block">
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <input
        ref={input}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        aria-label="Find a shipment"
        placeholder="Search shipments…"
        className="h-9 w-full rounded-lg border border-input bg-background pl-9 pr-10 text-[13px] placeholder:text-muted-foreground focus-visible:bg-card focus-visible:ring-offset-0"
      />
      <kbd aria-hidden="true" className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border bg-card px-1.5 text-[11px] font-medium text-muted-foreground">
        /
      </kbd>
    </form>
  )
}

function TopBar({ onMenu }: { onMenu: () => void }) {
  const location = useLocation()
  const where = whereAmI(location.pathname)

  return (
    <header className="sticky top-0 z-40 border-b bg-card/85 backdrop-blur supports-[backdrop-filter]:bg-card/70">
      <div className="mx-auto flex h-14 w-full max-w-[88rem] items-center gap-4 px-4 sm:px-8">
        <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation" onClick={onMenu}>
          <Menu />
        </Button>
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-[13px]">
          <span className="hidden text-muted-foreground sm:inline">{where.group}</span>
          <ChevronRight className="hidden size-3.5 text-muted-foreground sm:block" aria-hidden="true" />
          <span className={cn("truncate font-medium", where.detail && "text-muted-foreground")}>{where.page}</span>
          {where.detail && (
            <>
              <ChevronRight className="size-3.5 text-muted-foreground" aria-hidden="true" />
              <span className="truncate font-medium">{where.detail}</span>
            </>
          )}
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <ShipmentSearch />
          <SystemStatus />
        </div>
      </div>
    </header>
  )
}

function Sidebar({ session, onNavigate, children }: { session: Session; onNavigate?: () => void; children?: ReactNode }) {
  return (
    <>
      <div className="flex items-center justify-between pr-3">
        <Brand />
        {children}
      </div>
      <Navigation session={session} onNavigate={onNavigate} />
      <Account session={session} />
    </>
  )
}

export function AppShell({ session }: { session: Session }) {
  const [open, setOpen] = useState(false)
  const location = useLocation()

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[16.5rem_1fr]">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[70] focus:rounded-md focus:bg-card focus:px-3 focus:py-2 focus:text-sm focus:shadow-lg">
        Skip to content
      </a>

      <aside className="sticky top-0 hidden h-screen flex-col bg-navy-deep lg:flex" aria-label="Sidebar">
        <Sidebar session={session} />
      </aside>

      <div className="flex min-h-screen min-w-0 flex-col">
        <TopBar onMenu={() => setOpen(true)} />

        {open && (
          <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
            <div className="absolute inset-0 bg-navy-deep/50 backdrop-blur-[1px]" onClick={() => setOpen(false)} />
            <div className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-navy-deep shadow-raised">
              <Sidebar session={session} onNavigate={() => setOpen(false)}>
                <Button variant="ghost" size="icon" aria-label="Close navigation" className="text-navy-text hover:bg-white/10 hover:text-white" onClick={() => setOpen(false)}>
                  <X />
                </Button>
              </Sidebar>
            </div>
          </div>
        )}

        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[88rem] flex-1 px-4 py-6 sm:px-8 sm:py-8">
          <div key={location.pathname} className="page-enter">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}

export function Centered({ children }: { children: ReactNode }) {
  return <div className="grid min-h-screen place-items-center bg-background p-4">{children}</div>
}
