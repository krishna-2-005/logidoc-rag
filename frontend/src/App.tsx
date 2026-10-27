import { useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"
import { Navigate, Route, Routes } from "react-router-dom"
import { ErrorState, LoadingBlock } from "@/components/common/states"
import { AppShell, Centered } from "@/components/layout/AppShell"
import { useSession } from "@/hooks/queries"
import { UNAUTHORIZED_EVENT } from "@/lib/api"
import { Assistant } from "@/pages/Assistant"
import { Completeness } from "@/pages/Completeness"
import { Corrections } from "@/pages/Corrections"
import { Documents } from "@/pages/Documents"
import { Exceptions } from "@/pages/Exceptions"
import { FreightAudit } from "@/pages/FreightAudit"
import { AuditHistory } from "@/pages/History"
import { Invoices } from "@/pages/Invoices"
import { Login } from "@/pages/Login"
import { Overview } from "@/pages/Overview"
import { Reports } from "@/pages/Reports"
import { SettingsPage } from "@/pages/Settings"
import { ShipmentDetailPage } from "@/pages/ShipmentDetail"
import { Shipments } from "@/pages/Shipments"

export default function App() {
  const client = useQueryClient()
  const session = useSession()

  // The server ended the session (idle timeout, sign-out elsewhere, a deactivated account)
  useEffect(() => {
    const expired = () => client.invalidateQueries({ queryKey: ["session"] })
    window.addEventListener(UNAUTHORIZED_EVENT, expired)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, expired)
  }, [client])

  if (session.isPending) {
    return (
      <Centered>
        <div className="w-72">
          <LoadingBlock rows={2} label="Starting" />
        </div>
      </Centered>
    )
  }

  if (session.isError) {
    return (
      <Centered>
        <div className="w-full max-w-md">
          <ErrorState error={session.error} title="The server is not reachable" onRetry={() => session.refetch()} />
        </div>
      </Centered>
    )
  }

  if (!session.data.authenticated && session.data.mode === "required") return <Login />

  return (
    <Routes>
      <Route element={<AppShell session={session.data} />}>
        <Route index element={<Overview />} />
        <Route path="assistant" element={<Assistant />} />
        <Route path="shipments" element={<Shipments />} />
        <Route path="shipments/:id" element={<ShipmentDetailPage />} />
        <Route path="documents" element={<Documents />} />
        <Route path="freight-audit" element={<FreightAudit />} />
        <Route path="freight-audit/:id" element={<FreightAudit />} />
        <Route path="invoices" element={<Invoices />} />
        <Route path="exceptions" element={<Exceptions />} />
        <Route path="history" element={<AuditHistory />} />
        <Route path="corrections" element={<Corrections />} />
        <Route path="completeness" element={<Completeness />} />
        <Route path="reports" element={<Reports />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
