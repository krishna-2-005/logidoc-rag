import { RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react"
import { useRef, useState } from "react"
import { errorMessage } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { can, useIntegrity, useRecompute, useSession, useStaleness } from "@/hooks/queries"
import { label } from "@/lib/format"

type Outcome = { tone: "ok" | "info" | "bad"; text: string }

/**
 * What an audit field is called in the outdated cross-check notice. The shared label() (the one the
 * run inputs inspector uses) gives "Delivered pieces", "Invoice total"; only the two audit date ids,
 * which it would call "Expected date" and "Actual date", are spelled out. An unlisted id falls back
 * to its own words ("some_new_field" reads "Some new field").
 */
const AUDIT_FIELD_LABELS: Record<string, string> = {
  expected_date: "Expected delivery date",
  actual_date: "Actual delivery date",
}

export const auditFieldLabel = (id: string): string => AUDIT_FIELD_LABELS[id] ?? label(id)

/**
 * Warns when approved corrections cannot be read. The audit then answers from
 * the documents alone, and a reviewer must not take those values for approved ones.
 */
export function CorrectionStoreWarning() {
  const session = useSession()
  const staleness = useStaleness(can(session.data, "history.view"))

  if (staleness.data?.corrections_available !== false) return null

  return (
    <Alert tone="bad" className="items-start">
      <ShieldAlert />
      <div>
        <p className="font-medium">Corrections cannot be verified right now.</p>
        <p className="mt-0.5">
          The correction store could not be read. The values shown come from the documents; any approved correction is not reflected until it can be read again, so these values must not be taken as approved. Ask an administrator to check the extraction store.
        </p>
      </div>
    </Alert>
  )
}

/** The administrator's check of the stored history (hashes and approval rules). */
export function IntegrityCheck() {
  const session = useSession()
  const integrity = useIntegrity()

  if (!can(session.data, "admin.maintenance")) return null

  const report = integrity.data

  return (
    <div className="space-y-2">
      <Button variant="outline" size="sm" onClick={() => integrity.refetch()} disabled={integrity.isFetching}>
        <ShieldCheck /> {integrity.isFetching ? "Checking…" : "Check correction history integrity"}
      </Button>
      {integrity.isError && (
        <Alert tone="bad">
          <p>The integrity check could not run: {errorMessage(integrity.error)}</p>
        </Alert>
      )}
      {report?.ok && (
        <Alert tone="ok" role="status">
          <p>
            Verified {report.corrections_checked} correction(s), {report.decisions_checked} decision(s) and {report.extractions_checked} stored extraction(s).
            {report.unchained_legacy > 0 && ` ${report.unchained_legacy} older row(s) predate the hash chain and are not covered.`} This detects changes made with ordinary SQL; it is not tamper-proof against someone who can write the database file.
          </p>
        </Alert>
      )}
      {report && !report.ok && (
        <Alert tone="bad" className="items-start">
          <div>
            <p className="font-medium">The stored history failed verification.</p>
            <ul className="mt-1 list-disc pl-5">
              {report.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </div>
        </Alert>
      )}
    </div>
  )
}

/**
 * Whether the audit is ahead of its recorded history: approved corrections the
 * audit already reads that no recorded run includes yet. The retry records the
 * recalculated run; nothing here says it succeeded until the server does.
 */
export function RecomputeStatus({ shipmentKey }: { shipmentKey?: string }) {
  const session = useSession()
  const staleness = useStaleness(can(session.data, "history.view"))
  const recompute = useRecompute()
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [failed, setFailed] = useState(false)
  const busy = useRef(false)
  const canRecord = can(session.data, "history.record")
  // Only decides which words are shown: the server checks the permission on every request
  const canExtract = can(session.data, "extraction.run")

  const entries = (staleness.data?.stale ?? []).filter((item) => !shipmentKey || item.shipment_key === shipmentKey)
  const outdated = (staleness.data?.outdated_cross_checks ?? []).filter((item) => !shipmentKey || item.shipment_key === shipmentKey)

  const run = async (target?: string) => {
    if (busy.current) return
    busy.current = true
    setOutcome(null)

    try {
      const result = await recompute.mutateAsync(target)
      setFailed(false)
      setOutcome(
        result.recorded
          ? { tone: "ok", text: `Recorded run #${result.run_id} for ${result.shipments.join(", ")}. It supersedes the earlier run; that run and its inputs are unchanged.` }
          : { tone: "info", text: "Nothing needed recording: the recalculated run had already been recorded." },
      )
    } catch (error) {
      setFailed(true)
      setOutcome({ tone: "bad", text: `The run was not recorded: ${errorMessage(error)}` })
    } finally {
      busy.current = false
    }
  }

  if (!can(session.data, "history.view")) return null

  return (
    <div className="space-y-3" aria-live="polite">
      {staleness.isError && (
        <Alert tone="warn" className="items-start">
          <div>
            <p>Whether a recalculation is pending could not be checked: {errorMessage(staleness.error)}</p>
            <Button variant="outline" size="sm" className="mt-2 bg-card text-foreground" onClick={() => staleness.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </div>
        </Alert>
      )}

      {entries.map((item) => (
        <Alert key={item.shipment_key} tone="warn" className="items-start">
          <div className="flex-1">
            <p className="font-medium">A recalculated audit run is waiting to be recorded{shipmentKey ? "" : ` for ${item.shipment_key}`}.</p>
            <p className="mt-0.5">
              The latest recorded run (#{item.recorded_run_id}) does not include {item.missing_correction_ids.length} approved correction(s). The audit already uses them; the history does not show them yet.
            </p>
            {canRecord ? (
              <Button size="sm" className="mt-2" onClick={() => run(item.shipment_key)} disabled={recompute.isPending}>
                <RefreshCw /> {recompute.isPending ? "Recording…" : failed ? "Retry recording the run" : "Record the recalculated run"}
              </Button>
            ) : (
              <p className="mt-1 text-xs">An administrator can record it.</p>
            )}
          </div>
        </Alert>
      ))}

      {outdated.map((item) => (
        <Alert key={item.shipment_key} tone="warn" className="items-start" data-testid={`outdated-${item.shipment_key}`}>
          <div className="flex-1">
            <p className="font-medium">The structured cross-check is out of date{shipmentKey ? "" : ` for ${item.shipment_id}`}.</p>
            <p className="mt-0.5">
              Affected fields: {item.fields.map(auditFieldLabel).join(", ")}. The stored extraction was made by extractor version {item.extractor_versions.join(", ")}; the current version is {item.current_version}.
            </p>
            <p className="mt-0.5 text-xs">
              The audit values are read from the documents and are unchanged, and no audit run has been recalculated or recorded because of this. {canExtract
                ? "Run the extraction again for this shipment's documents (Extract fields) to refresh the comparison."
                : "You cannot run the extraction. Ask an authorized reviewer or administrator to run it again."}
            </p>
          </div>
        </Alert>
      ))}

      {outcome && (
        <Alert tone={outcome.tone} role={outcome.tone === "bad" ? "alert" : "status"}>
          <p>{outcome.text}</p>
        </Alert>
      )}

      {staleness.data?.corrections_available && entries.length === 0 && !outcome && (
        <p className="text-xs text-muted-foreground" role="status">
          No recorded run is waiting for an approved correction.
        </p>
      )}

      {canRecord && !shipmentKey && entries.length > 1 && (
        <Button variant="outline" size="sm" onClick={() => run(undefined)} disabled={recompute.isPending}>
          <RefreshCw /> {recompute.isPending ? "Recording…" : "Record every pending run"}
        </Button>
      )}

      <IntegrityCheck />
    </div>
  )
}
