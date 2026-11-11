import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { get, patch, post } from "@/lib/api"
import type { CarrierPage, CarrierRegistryDetail, DocumentNamePage, LinkQueue, LinkRequest, RegisteredCarrierRecord, RegistryEventPage } from "@/lib/types"

/**
 * The carrier registry screens. Every change is checked again by the server
 * (carriers.manage, the two-person rule); the screens only hide what the role
 * cannot do. An approved link changes how Carrier Intelligence groups
 * shipments, so changes also refresh everything under the "audit" key.
 */
const REGISTRY = "registry"

export interface CarrierQuery {
  search?: string
  status?: string[]
  sort?: string
  order?: "asc" | "desc"
  offset?: number
  limit?: number
}

export const useCarrierPage = (query: CarrierQuery) =>
  useQuery({ queryKey: [REGISTRY, "carriers", query], queryFn: () => get<CarrierPage>("/api/carrier-registry/carriers", { ...query }), placeholderData: (previous) => previous })

export const useRegistryCarrier = (id: number | null) =>
  useQuery({ queryKey: [REGISTRY, "carrier", id], queryFn: () => get<CarrierRegistryDetail>(`/api/carrier-registry/carriers/${id}`), enabled: id !== null })

export const useCarrierHistory = (id: number | null, offset: number, enabled: boolean) =>
  useQuery({
    queryKey: [REGISTRY, "history", id, offset],
    queryFn: () => get<RegistryEventPage>(`/api/carrier-registry/carriers/${id}/events`, { offset, limit: 50 }),
    enabled: enabled && id !== null,
    placeholderData: (previous) => previous,
  })

export const useLinkQueue = (state: "pending" | "decided" | "all", offset = 0) =>
  useQuery({ queryKey: [REGISTRY, "links", state, offset], queryFn: () => get<LinkQueue>("/api/carrier-registry/links", { state, offset, limit: 50 }), placeholderData: (previous) => previous })

export interface NameQuery {
  search?: string
  state?: "all" | "unlinked" | "pending" | "linked"
  offset?: number
  limit?: number
}

export const useDocumentNames = (query: NameQuery, enabled = true) =>
  useQuery({
    queryKey: [REGISTRY, "names", query],
    queryFn: () => get<DocumentNamePage>("/api/carrier-registry/document-names", { ...query }),
    placeholderData: (previous) => previous,
    enabled,
  })

/** Every registered carrier (for choosing one in a link proposal) */
export const useAllCarriers = (enabled = true) =>
  useQuery({ queryKey: [REGISTRY, "carriers", "all"], queryFn: () => get<CarrierPage>("/api/carrier-registry/carriers", { limit: 200 }), enabled })

function useRegistryChange<TArgs, TResult>(run: (args: TArgs) => Promise<TResult>) {
  const client = useQueryClient()

  return useMutation({
    mutationFn: run,
    onSuccess: () => {
      client.invalidateQueries({ queryKey: [REGISTRY] })
      client.invalidateQueries({ queryKey: ["audit"] })
    },
  })
}

export interface CarrierInput {
  legal_name?: string
  trading_name?: string | null
  scac?: string | null
  mc_number?: string | null
  dot_number?: string | null
  contact_name?: string | null
  contact_email?: string | null
  contact_phone?: string | null
  status?: string
  address_line1?: string | null
  address_line2?: string | null
  city?: string | null
  region?: string | null
  postal_code?: string | null
  country?: string | null
  confirm_distinct?: boolean
}

export const useRegisterCarrier = () => useRegistryChange((body: CarrierInput) => post<RegisteredCarrierRecord>("/api/carrier-registry", body))

export const useChangeCarrier = () =>
  useRegistryChange(({ id, ...body }: CarrierInput & { id: number }) => patch<RegisteredCarrierRecord>(`/api/carrier-registry/${id}`, body))

/** A proposal: no effect until a different administrator approves it. carrier_id null: unlink the name. */
export const useProposeLink = () =>
  useRegistryChange((body: { name: string; carrier_id: number | null; reason: string }) => post<LinkRequest>("/api/carrier-registry/links", body))

export const useDecideLink = () =>
  useRegistryChange(({ id, decision, note }: { id: number; decision: "approve" | "reject"; note?: string }) =>
    post<LinkRequest>(`/api/carrier-registry/links/${id}/decision`, { decision, note: note || undefined }),
  )
