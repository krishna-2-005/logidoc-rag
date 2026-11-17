import { useQueryClient } from "@tanstack/react-query"
import { lazy, useEffect } from "react"
import { Navigate, Route, Routes } from "react-router-dom"
import { ErrorState, LoadingBlock } from "@/components/common/states"
import { AppShell, Centered } from "@/components/layout/AppShell"
import { useSession } from "@/hooks/queries"
import { UNAUTHORIZED_EVENT } from "@/lib/api"
import { Login } from "@/pages/Login"

// Every page is its own chunk, fetched when its route is first opened (the sign-in page and the shell are in
// the first load). AppShell wraps the routed page in a RouteBoundary: a loading state, and an error on failure.
const Assistant = lazy(() => import("@/pages/Assistant").then((module) => ({ default: module.Assistant })))
const CarrierIntelligence = lazy(() => import("@/pages/CarrierIntelligence").then((module) => ({ default: module.CarrierIntelligence })))
const CarrierLinks = lazy(() => import("@/pages/CarrierLinks").then((module) => ({ default: module.CarrierLinks })))
const CarrierRegistry = lazy(() => import("@/pages/CarrierRegistry").then((module) => ({ default: module.CarrierRegistry })))
const CarrierRegistryDetail = lazy(() => import("@/pages/CarrierRegistryDetail").then((module) => ({ default: module.CarrierRegistryDetail })))
const IntegrationDetail = lazy(() => import("@/pages/IntegrationDetail").then((module) => ({ default: module.IntegrationDetail })))
const Integrations = lazy(() => import("@/pages/Integrations").then((module) => ({ default: module.Integrations })))
const ScheduledJobs = lazy(() => import("@/pages/ScheduledJobs").then((module) => ({ default: module.ScheduledJobs })))
const WebhookDetail = lazy(() => import("@/pages/WebhookDetail").then((module) => ({ default: module.WebhookDetail })))
const DeliveryDetail = lazy(() => import("@/pages/WebhookDetail").then((module) => ({ default: module.DeliveryDetail })))
const Webhooks = lazy(() => import("@/pages/Webhooks").then((module) => ({ default: module.Webhooks })))
const Completeness = lazy(() => import("@/pages/Completeness").then((module) => ({ default: module.Completeness })))
const Corrections = lazy(() => import("@/pages/Corrections").then((module) => ({ default: module.Corrections })))
const Documents = lazy(() => import("@/pages/Documents").then((module) => ({ default: module.Documents })))
const Exceptions = lazy(() => import("@/pages/Exceptions").then((module) => ({ default: module.Exceptions })))
const FreightAudit = lazy(() => import("@/pages/FreightAudit").then((module) => ({ default: module.FreightAudit })))
const AuditHistory = lazy(() => import("@/pages/History").then((module) => ({ default: module.AuditHistory })))
const Invoices = lazy(() => import("@/pages/Invoices").then((module) => ({ default: module.Invoices })))
const Overview = lazy(() => import("@/pages/Overview").then((module) => ({ default: module.Overview })))
const Reports = lazy(() => import("@/pages/Reports").then((module) => ({ default: module.Reports })))
const Risk = lazy(() => import("@/pages/Risk").then((module) => ({ default: module.Risk })))
const SettingsPage = lazy(() => import("@/pages/Settings").then((module) => ({ default: module.SettingsPage })))
const ShipmentDetailPage = lazy(() => import("@/pages/ShipmentDetail").then((module) => ({ default: module.ShipmentDetailPage })))
const Shipments = lazy(() => import("@/pages/Shipments").then((module) => ({ default: module.Shipments })))

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
        <Route path="carriers" element={<CarrierIntelligence />} />
        <Route path="risk" element={<Risk />} />
        <Route path="carrier-registry" element={<CarrierRegistry />} />
        <Route path="carrier-registry/links" element={<CarrierLinks />} />
        <Route path="carrier-registry/:id" element={<CarrierRegistryDetail />} />
        <Route path="integrations" element={<Integrations />} />
        <Route path="integrations/connections/:id" element={<IntegrationDetail />} />
        <Route path="integrations/webhooks" element={<Webhooks />} />
        <Route path="integrations/webhooks/:id" element={<WebhookDetail />} />
        <Route path="integrations/deliveries/:id" element={<DeliveryDetail />} />
        <Route path="integrations/jobs" element={<ScheduledJobs />} />
        <Route path="corrections" element={<Corrections />} />
        <Route path="completeness" element={<Completeness />} />
        <Route path="reports" element={<Reports />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
