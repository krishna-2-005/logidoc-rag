import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { get, patch, post } from "@/lib/api"
import type {
  ConnectionTest,
  DeliveryPage,
  ImportedRecordDetail,
  Integration,
  IntegrationDashboard,
  IntegrationDetail,
  IntegrationList,
  JobRunPage,
  RecordPage,
  ScheduledJob,
  SchedulerStatus,
  SyncRunDetail,
  SyncRunPage,
  WebhookConfiguration,
  WebhookDeliveryDetail,
  WebhookEndpoint,
  WebhookEndpointDetail,
  WebhookMonitoring,
  WebhookSecretIssued,
} from "@/lib/types"

/**
 * Webhooks, scheduled jobs and TMS/ERP integrations (administrators only; the
 * server refuses anyone else). Lists that change on their own (deliveries,
 * runs) refresh every 15 seconds while a screen shows them.
 */
const KEY = "integrations"
const LIVE = 15_000

export const useWebhookSettings = () => useQuery({ queryKey: [KEY, "webhook-settings"], queryFn: () => get<WebhookConfiguration>("/api/integrations/webhooks/settings") })

export const useEndpoints = (includeArchived: boolean) =>
  useQuery({ queryKey: [KEY, "endpoints", includeArchived], queryFn: () => get<{ endpoints: WebhookEndpoint[] }>("/api/integrations/webhooks", { include_archived: includeArchived }), refetchInterval: LIVE })

export const useEndpoint = (id: number | null) =>
  useQuery({ queryKey: [KEY, "endpoint", id], queryFn: () => get<WebhookEndpointDetail>(`/api/integrations/webhooks/${id}`), enabled: id !== null, refetchInterval: LIVE })

export interface DeliveryFilters {
  endpoint_id?: number
  status?: string[]
  event_type?: string[]
  /** Created on or after this day (YYYY-MM-DD, UTC) */
  from?: string
  /** Created on or before this day */
  to?: string
  offset?: number
  limit?: number
}

/** GET /api/integrations/monitoring/webhooks: recorded deliveries over the last `days` days */
export const useWebhookMonitoring = (days: number, endpointId?: number, eventType?: string) =>
  useQuery({
    queryKey: [KEY, "webhook-monitoring", days, endpointId ?? null, eventType ?? null],
    queryFn: () => get<WebhookMonitoring>("/api/integrations/monitoring/webhooks", { days, endpoint_id: endpointId, event_type: eventType }),
    placeholderData: (previous) => previous,
    refetchInterval: LIVE,
  })

/** GET /api/integrations/monitoring/connections: recorded sync runs over the last `days` days */
export const useIntegrationDashboard = (days: number) =>
  useQuery({
    queryKey: [KEY, "integration-dashboard", days],
    queryFn: () => get<IntegrationDashboard>("/api/integrations/monitoring/connections", { days }),
    placeholderData: (previous) => previous,
    refetchInterval: LIVE,
  })

export const useDeliveries = (filters: DeliveryFilters) =>
  useQuery({ queryKey: [KEY, "deliveries", filters], queryFn: () => get<DeliveryPage>("/api/integrations/deliveries", { ...filters }), placeholderData: (previous) => previous, refetchInterval: LIVE })

export const useDelivery = (id: number | null) =>
  useQuery({ queryKey: [KEY, "delivery", id], queryFn: () => get<WebhookDeliveryDetail>(`/api/integrations/deliveries/${id}`), enabled: id !== null, refetchInterval: LIVE })

export const useSchedulerStatus = () => useQuery({ queryKey: [KEY, "jobs"], queryFn: () => get<SchedulerStatus>("/api/integrations/jobs"), refetchInterval: LIVE })

export const useJobRuns = (name: string | null, offset = 0) =>
  useQuery({
    queryKey: [KEY, "job-runs", name, offset],
    queryFn: () => get<JobRunPage>(`/api/integrations/jobs/${encodeURIComponent(name ?? "")}/runs`, { offset, limit: 20 }),
    enabled: !!name,
    placeholderData: (previous) => previous,
    refetchInterval: LIVE,
  })

export const useIntegrations = () => useQuery({ queryKey: [KEY, "connections"], queryFn: () => get<IntegrationList>("/api/integrations/connections"), refetchInterval: LIVE })

export const useIntegration = (id: number | null) =>
  useQuery({ queryKey: [KEY, "connection", id], queryFn: () => get<IntegrationDetail>(`/api/integrations/connections/${id}`), enabled: id !== null, refetchInterval: LIVE })

export const useSyncRuns = (id: number | null, offset = 0) =>
  useQuery({
    queryKey: [KEY, "sync-runs", id, offset],
    queryFn: () => get<SyncRunPage>(`/api/integrations/connections/${id}/runs`, { offset, limit: 20 }),
    enabled: id !== null,
    placeholderData: (previous) => previous,
    refetchInterval: LIVE,
  })

export const useSyncRun = (id: number | null) =>
  useQuery({ queryKey: [KEY, "sync-run", id], queryFn: () => get<SyncRunDetail>(`/api/integrations/sync-runs/${id}`), enabled: id !== null })

export const useRecords = (id: number | null, filters: { entity?: string; search?: string; offset?: number; limit?: number }) =>
  useQuery({
    queryKey: [KEY, "records", id, filters],
    queryFn: () => get<RecordPage>(`/api/integrations/connections/${id}/records`, { ...filters }),
    enabled: id !== null,
    placeholderData: (previous) => previous,
  })

export const useRecord = (integrationId: number | null, recordId: number | null) =>
  useQuery({
    queryKey: [KEY, "record", integrationId, recordId],
    queryFn: () => get<ImportedRecordDetail>(`/api/integrations/connections/${integrationId}/records/${recordId}`),
    enabled: integrationId !== null && recordId !== null,
  })

function useChange<TArgs, TResult>(run: (args: TArgs) => Promise<TResult>) {
  const client = useQueryClient()

  return useMutation({ mutationFn: run, onSuccess: () => client.invalidateQueries({ queryKey: [KEY] }) })
}

export interface EndpointInput {
  name?: string
  url?: string
  event_types?: string[]
  enabled?: boolean
}

export const useCreateEndpoint = () => useChange((body: EndpointInput) => post<WebhookSecretIssued>("/api/integrations/webhooks", body))
export const useChangeEndpoint = () => useChange(({ id, ...body }: EndpointInput & { id: number }) => patch<WebhookEndpoint>(`/api/integrations/webhooks/${id}`, body))
export const useRotateSecret = () => useChange((id: number) => post<WebhookSecretIssued>(`/api/integrations/webhooks/${id}/rotate-secret`))
export const useArchiveEndpoint = () => useChange((id: number) => post<WebhookEndpoint>(`/api/integrations/webhooks/${id}/archive`))
export const useTestEndpoint = () => useChange((id: number) => post<WebhookDeliveryDetail>(`/api/integrations/webhooks/${id}/test`))
export const useRetryDelivery = () => useChange((id: number) => post<WebhookDeliveryDetail>(`/api/integrations/deliveries/${id}/retry`))

export const useChangeJob = () =>
  useChange(({ name, ...body }: { name: string; enabled?: boolean; interval_seconds?: number }) => patch<ScheduledJob>(`/api/integrations/jobs/${encodeURIComponent(name)}`, body))
export const useRunJob = () =>
  useChange(({ name, retry }: { name: string; retry?: boolean }) => post<{ run_id: number }>(`/api/integrations/jobs/${encodeURIComponent(name)}/${retry ? "retry" : "run"}`))

export interface IntegrationInput {
  name?: string
  provider?: string
  settings?: Record<string, unknown>
  credential_env?: string | null
  sync_interval_seconds?: number
  enabled?: boolean
}

export const useCreateIntegration = () => useChange((body: IntegrationInput) => post<Integration>("/api/integrations/connections", body))
export const useChangeIntegration = () => useChange(({ id, ...body }: IntegrationInput & { id: number }) => patch<Integration>(`/api/integrations/connections/${id}`, body))
export const useTestIntegration = () => useChange((id: number) => post<ConnectionTest>(`/api/integrations/connections/${id}/test`))
export const useSyncNow = () => useChange((id: number) => post<{ run_id: number }>(`/api/integrations/connections/${id}/sync`))
export const useSimulateChanges = () => useChange(({ id, count }: { id: number; count: number }) => post<{ changed: string[] }>(`/api/integrations/connections/${id}/sandbox/changes`, { count }))
export const useCorrectRecord = () =>
  useChange(({ integrationId, recordId, ...body }: { integrationId: number; recordId: number; field: string; value?: unknown; reason: string; clear?: boolean }) =>
    post<ImportedRecordDetail>(`/api/integrations/connections/${integrationId}/records/${recordId}/corrections`, body),
  )
