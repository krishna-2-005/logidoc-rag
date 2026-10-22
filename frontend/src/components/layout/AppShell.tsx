import {
  BadgeCheck,
  ClipboardCheck,
  FileCheck2,
  FileDown,
  FileSearch,
  History,
  LayoutDashboard,
  LogOut,
  Bot,
  Menu,
  PackageSearch,
  Scale,
  Settings as SettingsIcon,
  TriangleAlert,
  X,
} from "lucide-react"
import { useState, type ReactNode } from "react"
import { NavLink, Outlet, useLocation } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { can, useLogout } from "@/hooks/queries"
import type { Session } from "@/lib/types"
import { cn } from "@/lib/utils"

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/assistant", label: "AI Assistant", icon: Bot },
  { to: "/shipments", label: "Shipment Explorer", icon: PackageSearch },
  { to: "/documents", label: "Document Intelligence", icon: FileSearch },
  { to: "/freight-audit", label: "Freight Audit", icon: Scale },
  { to: "/exceptions", label: "Exception Center", icon: ClipboardCheck },
  { to: "/history", label: "Audit History", icon: History },
  { to: "/corrections", label: "Correction Approvals", icon: BadgeCheck, permission: "extraction.approve" },
  { to: "/completeness", label: "Document Completeness", icon: FileCheck2 },
  { to: "/reports", label: "Reports", icon: FileDown },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
] as const

function Brand() {
  return (
    <div className="flex items-center gap-2.5 px-5 py-5">
      <span aria-hidden="true" className="grid size-8 place-items-center rounded-lg bg-navy-line text-teal-300">
        <svg viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
          <path d="M3 8.5 12 4l9 4.5v7L12 20l-9-4.5z" />
          <path d="M3 8.5 12 13l9-4.5M12 13v7" />
        </svg>
      </span>
      <div>
        <p className="text-sm font-semibold leading-none text-white">LogiDoc</p>
        <p className="mt-1 text-[11px] leading-none text-navy-text/70">Freight audit intelligence</p>
      </div>
    </div>
  )
}

function Navigation({ session, onNavigate }: { session: Session; onNavigate?: () => void }) {
  return (
    <nav aria-label="Main" className="flex-1 space-y-0.5 overflow-y-auto px-3">
      {NAV.filter((item) => !("permission" in item) || can(session, item.permission)).map(({ to, label, icon: Icon, ...rest }) => (
        <NavLink
          key={to}
          to={to}
          end={"end" in rest ? rest.end : false}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "group flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-navy-text/85 transition-colors hover:bg-navy-line/60 hover:text-white",
              isActive && "bg-navy-line text-white shadow-sm",
            )
          }
        >
          <Icon className="size-4 shrink-0" aria-hidden="true" />
          {label}
        </NavLink>
      ))}
    </nav>
  )
}

function Account({ session }: { session: Session }) {
  const logout = useLogout()

  return (
    <div className="border-t border-navy-line p-4 text-xs">
      {session.mode === "local" ? (
        <div className="flex gap-2 rounded-md bg-warn-soft/10 p-2.5 text-[11px] leading-snug text-amber-200">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          <p>Local development mode. Nobody signs in; actions are recorded as not authenticated.</p>
        </div>
      ) : (
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate font-medium text-white">{session.user?.username}</p>
            <p className="truncate text-navy-text/70">{session.user?.label}</p>
          </div>
          <Button variant="ghost" size="icon" className="text-navy-text hover:bg-navy-line hover:text-white" aria-label="Sign out" onClick={() => logout.mutate()}>
            <LogOut />
          </Button>
        </div>
      )}
    </div>
  )
}

export function AppShell({ session }: { session: Session }) {
  const [open, setOpen] = useState(false)
  const location = useLocation()

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[17rem_1fr]">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[70] focus:rounded-md focus:bg-card focus:px-3 focus:py-2 focus:text-sm focus:shadow-lg">
        Skip to content
      </a>

      <aside className="sticky top-0 hidden h-screen flex-col bg-navy lg:flex" aria-label="Sidebar">
        <Brand />
        <Navigation session={session} />
        <Account session={session} />
      </aside>

      <div className="flex min-h-screen min-w-0 flex-col">
        <div className="sticky top-0 z-40 flex items-center justify-between bg-navy px-4 py-2.5 lg:hidden">
          <span className="text-sm font-semibold text-white">LogiDoc</span>
          <Button variant="ghost" size="icon" className="text-white hover:bg-navy-line hover:text-white" aria-label="Open navigation" aria-expanded={open} onClick={() => setOpen(true)}>
            <Menu />
          </Button>
        </div>

        {open && (
          <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
            <div className="absolute inset-0 bg-navy-deep/60" onClick={() => setOpen(false)} />
            <div className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-navy">
              <div className="flex items-center justify-between pr-3">
                <Brand />
                <Button variant="ghost" size="icon" className="text-white hover:bg-navy-line hover:text-white" aria-label="Close navigation" onClick={() => setOpen(false)}>
                  <X />
                </Button>
              </div>
              <Navigation session={session} onNavigate={() => setOpen(false)} />
              <Account session={session} />
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
