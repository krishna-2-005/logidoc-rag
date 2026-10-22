import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { get, patch, post, postForm } from "@/lib/api"
import type {
  AuditRun,
  Correction,
  DecisionResult,
  ExtractionDetail,
  ExtractionList,
  IntegrityReport,
  PendingList,
  RecomputeResult,
  RunInputs,
  Staleness,
  AssistantReply,
  ChatReply,
  Completeness,
  DocumentsPayload,
  ExceptionDetail,
  ExceptionList,
  Exposure,
  IngestionRecord,
  IngestionRegister,
  Overview,
  Session,
  Settings,
  ShipmentDetail,
  ShipmentList,
  TimelineEntry,
  Trends,
  UploadSummary,
  UserAccount,
  WorkflowAction,
  ReviewRow,
} from "@/lib/types"

/**
 * Everything derived from the documents lives under the "audit" key, so one
 * invalidation after a change (an upload, a review, a recorded run) refreshes
 * every screen that could be affected.
 */
const AUDIT = "audit"

export function useSession() {
  return useQuery({ queryKey: ["session"], queryFn: () => get<Session>("/api/session"), staleTime: 30_000, retry: false })
}

export function can(session: Session | undefined, permission: string): boolean {
  return !!session?.permissions.includes(permission)
}

export const useOverview = () => useQuery({ queryKey: [AUDIT, "overview"], queryFn: () => get<Overview>("/api/overview") })

export interface ShipmentFilters {
  search?: string
  carrier?: string[]
  status?: string[]
  category?: string[]
  invoice_mismatch?: boolean
  delivery_delayed?: boolean
  needs_review?: boolean
}

export const useShipments = (filters: ShipmentFilters) =>
  useQuery({ queryKey: [AUDIT, "shipments", filters], queryFn: () => get<ShipmentList>("/api/shipments", { ...filters }), placeholderData: (previous) => previous })

export const useShipment = (id: string | undefined) =>
  useQuery({ queryKey: [AUDIT, "shipment", id], queryFn: () => get<ShipmentDetail>(`/api/shipments/${encodeURIComponent(id ?? "")}`), enabled: !!id })

export const useDocuments = () => useQuery({ queryKey: [AUDIT, "documents"], queryFn: () => get<DocumentsPayload>("/api/documents") })

export const useCompleteness = (search: string, incompleteOnly: boolean) =>
  useQuery({
    queryKey: [AUDIT, "completeness", search, incompleteOnly],
    queryFn: () => get<Completeness>("/api/completeness", { search, incomplete_only: incompleteOnly }),
    placeholderData: (previous) => previous,
  })

export const useExposure = () => useQuery({ queryKey: [AUDIT, "exposure"], queryFn: () => get<Exposure>("/api/exposure") })

export interface ExceptionFilters {
  search?: string
  assignee?: string
  unassigned?: boolean
  status?: string[]
  category?: string[]
  min_age_days?: number
  max_age_days?: number
  pending_approval?: boolean
}

export const useExceptions = (filters: ExceptionFilters) =>
  useQuery({ queryKey: [AUDIT, "exceptions", filters], queryFn: () => get<ExceptionList>("/api/exceptions", { ...filters }), placeholderData: (previous) => previous })

export const useExceptionDetail = (key: string | null) =>
  useQuery({
    queryKey: [AUDIT, "exception", key],
    queryFn: () => get<ExceptionDetail>(`/api/exceptions/${encodeURIComponent(key ?? "")}`),
    enabled: !!key,
  })

export interface TrendFilters {
  period: string
  carrier?: string[]
  status?: string[]
  category?: string[]
  from?: string
  to?: string
  min_sample?: number
}

export const useTrends = (filters: TrendFilters) =>
  useQuery({ queryKey: [AUDIT, "trends", filters], queryFn: () => get<Trends>("/api/trends", { ...filters }), placeholderData: (previous) => previous })

export const useRuns = () => useQuery({ queryKey: [AUDIT, "runs"], queryFn: () => get<AuditRun[]>("/api/history/runs") })

export const useShipmentHistory = (id: string | undefined) =>
  useQuery({ queryKey: [AUDIT, "history", id], queryFn: () => get<TimelineEntry[]>(`/api/history/shipments/${encodeURIComponent(id ?? "")}`), enabled: !!id })

export const useIngestion = (status: string[], search: string) =>
  useQuery({
    queryKey: [AUDIT, "ingestion", status, search],
    queryFn: () => get<IngestionRegister>("/api/ingestion/register", { status, search }),
    placeholderData: (previous) => previous,
  })

export const useIngestionErrors = () =>
  useQuery({ queryKey: [AUDIT, "ingestion-errors"], queryFn: () => get<(IngestionRecord & { can_retry: boolean })[]>("/api/ingestion/errors") })

export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: () => get<Settings>("/api/settings") })

export const useUsers = (enabled: boolean) => useQuery({ queryKey: ["users"], queryFn: () => get<UserAccount[]>("/api/admin/users"), enabled })

// ---------------------------------------------------------------------------
// Changes
// ---------------------------------------------------------------------------

function useInvalidating<TArgs, TResult>(run: (args: TArgs) => Promise<TResult>, keys: string[] = [AUDIT]) {
  const client = useQueryClient()

  return useMutation({
    mutationFn: run,
    onSuccess: () => {
      keys.forEach((key) => client.invalidateQueries({ queryKey: [key] }))
    },
  })
}

export interface ActionInput {
  key: string
  action: WorkflowAction
  note?: string
  evidence?: string[]
  assignee?: string
}

export const useExceptionAction = () =>
  useInvalidating(({ key, ...body }: ActionInput) =>
    post<{ review: ReviewRow; allowed_actions: WorkflowAction[] }>(`/api/exceptions/${encodeURIComponent(key)}/actions`, body),
  )

export const useRecordRun = () => useInvalidating((label?: string) => post<{ run_id: number; snapshots_written: number; shipments_checked: number }>("/api/history/runs", { label }))

export const useUpload = () =>
  useInvalidating((files: File[]) => {
    const form = new FormData()
    files.forEach((file) => form.append("files", file))
    return postForm<UploadSummary>("/api/ingestion/upload", form)
  })

export const useRetry = () => useInvalidating((id: number) => post<UploadSummary>(`/api/ingestion/${id}/retry`))

export const useResolveError = () => useInvalidating(({ id, note }: { id: number; note: string }) => post<IngestionRecord>(`/api/ingestion/${id}/resolve`, { note }))

export const useReprocess = () => useInvalidating((name: string) => post<{ action: string; chunks: number }>("/api/documents/reprocess", undefined, { name }))

export const useReplace = () =>
  useInvalidating(({ name, file }: { name: string; file: File }) => {
    const form = new FormData()
    form.append("file", file)
    return postForm<{ action: string; archived: string | null }>("/api/documents/replace", form, { name })
  })

export const useRemove = () => useInvalidating((name: string) => post<{ archived: string; vectors_removed: number }>("/api/documents/remove", undefined, { name, confirm: true }))

export const useChat = () => useMutation({ mutationFn: (body: { question: string; shipment_id?: string }) => post<ChatReply>("/api/chat", body) })

export function useLogin() {
  const client = useQueryClient()

  return useMutation({
    mutationFn: (body: { username: string; password: string }) => post<Session>("/api/auth/login", body),
    onSuccess: (session) => {
      // Nothing cached for a previous user may be shown to this one. The session
      // query itself must stay: the mounted app is observing it.
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== "session" })
      client.setQueryData(["session"], session)
    },
  })
}

export function useLogout() {
  const client = useQueryClient()

  return useMutation({
    mutationFn: () => post<{ authenticated: boolean }>("/api/auth/logout"),
    onSettled: () => {
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== "session" })
      client.invalidateQueries({ queryKey: ["session"] })
    },
  })
}

export const useCreateUser = () => useInvalidating((body: { username: string; password: string; role: string }) => post("/api/admin/users", body), ["users"])

export const useChangeUser = () =>
  useInvalidating(({ username, ...body }: { username: string; role?: string; active?: boolean; password?: string }) => patch(`/api/admin/users/${encodeURIComponent(username)}`, body), ["users"])

export const useAssistant = () =>
  useMutation({ mutationFn: (body: { question: string; shipment_id?: string }) => post<AssistantReply>("/api/assistant", body) })

// ---------------------------------------------------------------------------
// Structured extraction, correction approval and recorded audit inputs
// ---------------------------------------------------------------------------

export const useExtractions = (shipmentId?: string, enabled = true) =>
  useQuery({
    queryKey: [AUDIT, "extractions", shipmentId ?? "all"],
    queryFn: () => get<ExtractionList>("/api/extractions", { shipment_id: shipmentId }),
    enabled,
  })

export const useExtraction = (id: number | null) =>
  useQuery({ queryKey: [AUDIT, "extraction", id], queryFn: () => get<ExtractionDetail>(`/api/extractions/${id}`), enabled: id !== null })

export const usePendingCorrections = (enabled: boolean) =>
  useQuery({ queryKey: [AUDIT, "corrections-pending"], queryFn: () => get<PendingList>("/api/extractions/pending"), enabled })

export const useStaleness = (enabled: boolean) =>
  useQuery({ queryKey: [AUDIT, "staleness"], queryFn: () => get<Staleness>("/api/extractions/staleness"), enabled })

/** Runs only when asked (refetch): it reads every stored hash. */
export const useIntegrity = () =>
  useQuery({ queryKey: [AUDIT, "integrity"], queryFn: () => get<IntegrityReport>("/api/extractions/integrity"), enabled: false, retry: false })

export const useRunInputs = (runId: number | null) =>
  useQuery({ queryKey: [AUDIT, "run-inputs", runId], queryFn: () => get<RunInputs>(`/api/history/runs/${runId}/inputs`), enabled: runId !== null })

export const useRunExtraction = () =>
  useInvalidating((document: string) =>
    post<{ id: number; created: boolean; status: string; complete: boolean; review_reasons: string[] }>("/api/extractions/run", { document }),
  )

export interface ProposeInput {
  extractionId: number
  field: string
  value: string
  reason: string
  basedOn: number | null
}

/** A proposal: it has no effect on any audit until a different administrator approves it. */
export const useProposeCorrection = () =>
  useInvalidating(({ extractionId, field, value, reason, basedOn }: ProposeInput) =>
    post<Correction>(`/api/extractions/${extractionId}/corrections`, { field, value, reason, based_on: basedOn }),
  )

export const useDecideCorrection = () =>
  useInvalidating(({ id, decision, note }: { id: number; decision: "approve" | "reject"; note?: string }) =>
    post<DecisionResult>(`/api/extractions/corrections/${id}/decision`, { decision, note: note || undefined }),
  )

/** Records the recalculated run for the shipment (or every stale shipment). Safe to repeat. */
export const useRecompute = () => useInvalidating((shipmentId?: string) => post<RecomputeResult>("/api/extractions/recompute", { shipment_id: shipmentId }))
