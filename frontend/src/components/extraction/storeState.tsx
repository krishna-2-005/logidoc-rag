import { DatabaseZap } from "lucide-react"
import type { ReactNode } from "react"
import { Alert } from "@/components/ui/alert"
import { can, useSession, useStaleness } from "@/hooks/queries"
import { dateTime } from "@/lib/format"
import type { Staleness, StoreMigration } from "@/lib/types"

/**
 * What the server says the extraction store is (GET /api/extractions/staleness, "store").
 *
 * The wording is fixed on purpose: the server's "reason" text is never shown. A migration failure is shown only when the
 * server confirms it from the migration command's durable record for THIS store ("migration.attempt" is "failed" and the
 * record is "ok"; the server only sends that when the record matches the database's history). Anything else about a
 * record (missing, unreadable, from another store, out of date) is shown as what it is, never as a failure. A state this
 * page does not know is treated as not writable, and a missing "store" object says nothing at all.
 */
type Known = "absent" | "older" | "migration_pending" | "current" | "unavailable" | "migration_failed"

const WRITABLE: Known[] = ["absent", "current"]

const KNOWN: Known[] = ["absent", "older", "migration_pending", "current", "unavailable", "migration_failed"]

const isKnown = (state: string): state is Known => (KNOWN as string[]).includes(state)

/** Why extraction writes are unavailable, or null when they are not known to be (including when nothing is known). */
export function storeWriteBlock(staleness: Staleness | undefined): string | null {
  const state = staleness?.store?.state

  if (!state) return null
  if (isKnown(state) && WRITABLE.includes(state)) return null

  return "The extraction store is not ready for changes. Extracting, proposing and deciding corrections are unavailable until it is."
}

/** Disables writes while the server reports a store that cannot take them. The server checks again on every request. */
export function useStoreWriteBlock(): string | null {
  const session = useSession()

  return storeWriteBlock(useStaleness(can(session.data, "history.view")).data)
}

/** Recovery guidance for each fixed failure code. No code here offers an action in the page: migration is an operator command. */
const FAILURE_GUIDANCE: Record<string, string> = {
  history_verification_failed: "The correction history did not pass verification, so the migration refused to run. The history must be repaired or restored before it can be migrated.",
  integrity_check_failed: "The database failed its integrity check, so the migration refused to run. It must be repaired or restored first.",
  backup_failed: "The backup could not be made and verified, so nothing was changed. Check the backup destination before running the migration again.",
  store_changed: "The store changed while the migration was starting, so nothing was changed. Run the migration again when nothing else is writing to it.",
  rolled_back: "The migration stopped part-way and was rolled back. Check the cause before running it again.",
  verification_failed: "The migration's own checks failed before it committed, so it was rolled back. Check the cause before running it again.",
}

const GENERIC_FAILURE = "The migration did not complete, and the store was left as it was."

function Notice({ tone, id, title, children }: { tone: "info" | "warn" | "bad"; id: string; title?: string; children: ReactNode }) {
  return (
    <Alert tone={tone} className="items-start" data-testid={`store-state-${id}`}>
      <DatabaseZap aria-hidden="true" />
      <div>
        {title && <p className="font-medium">{title}</p>}
        {children}
      </div>
    </Alert>
  )
}

/** One sentence about what the migration record does and does not say, for a store that is not current. */
function RecordNote({ migration }: { migration: StoreMigration | null | undefined }) {
  if (!migration) return null

  if (migration.attempt === "unfinished") {
    return (
      <p className="mt-0.5 text-xs" data-testid="migration-record-note">
        A migration was started{migration.at ? ` (${dateTime(migration.at)})` : ""} and has not reported an outcome: it may still be running, or it may have been interrupted. Check with the operator before starting another. This is not a confirmed failure.
      </p>
    )
  }

  if (migration.record === "unreadable") {
    return (
      <p className="mt-0.5 text-xs" data-testid="migration-record-note">
        A migration status record exists but could not be read, so this page makes no claim about earlier attempts.
      </p>
    )
  }

  if (migration.record === "ignored") {
    return (
      <p className="mt-0.5 text-xs" data-testid="migration-record-note">
        An older migration status record does not apply to this store as it is now and was ignored.
      </p>
    )
  }

  if (migration.record === "missing") {
    return (
      <p className="mt-0.5 text-xs" data-testid="migration-record-note">
        No migration attempt has been recorded for this store.
      </p>
    )
  }

  return null
}

/** The notice for each state. Nothing is shown for a store that is current. */
export function StoreStateNotice() {
  const session = useSession()
  const staleness = useStaleness(can(session.data, "history.view"))
  const store = staleness.data?.store

  if (!store) return null

  const state: string = store.state
  const migration = store.migration

  if (state === "current") return null

  if (state === "absent") {
    return (
      <Notice tone="info" id="absent">
        <p>No extraction store exists yet. The first extraction creates it.</p>
      </Notice>
    )
  }

  if (state === "migration_failed") {
    // Only a failure the server confirmed from this store's own record is called a failure
    if (migration?.attempt === "failed" && migration.record === "ok") {
      return (
        <Notice tone="warn" id="migration_failed" title="A migration of the extraction store was attempted and failed.">
          <p className="mt-0.5">
            {FAILURE_GUIDANCE[migration.code ?? ""] ?? GENERIC_FAILURE}
            {migration.at ? ` It was recorded ${dateTime(migration.at)}.` : ""} The store is as it was: stored extractions, approved corrections and their history are still read, and audits are unaffected.
          </p>
          <p className="mt-0.5">
            Extracting documents, proposing corrections and deciding them are unavailable until an operator has dealt with the cause and run the migration again. It is not run from this page.
          </p>
        </Notice>
      )
    }

    return (
      <Notice tone="warn" id="migration_failed_unconfirmed" title="The extraction store is waiting for an explicit migration.">
        <p className="mt-0.5">The server reports a migration problem but could not confirm it from a status record for this store, so no failure is claimed. Writes are unavailable until an operator checks it.</p>
      </Notice>
    )
  }

  if (state === "migration_pending") {
    return (
      <Notice tone="warn" id="migration_pending" title="The extraction store is waiting for an explicit migration.">
        <p className="mt-0.5">
          Stored extractions, approved corrections and their history are still read, and audits use the approved corrections as before. Extracting documents, proposing corrections and deciding them are unavailable until an operator runs the migration; it is not run from this page, and viewing this page never starts it. File-name records are not available yet, which does not mean there are none.
        </p>
        <RecordNote migration={migration} />
      </Notice>
    )
  }

  if (state === "older") {
    return (
      <Notice tone="warn" id="older" title="The extraction store is from before approvals existed and needs an explicit migration.">
        <p className="mt-0.5">
          It cannot be listed or changed from here until an operator runs the migration. No approved correction can exist in a store this old, so audits are not affected.
        </p>
        <RecordNote migration={migration} />
      </Notice>
    )
  }

  if (state === "unavailable") {
    if (migration?.attempt === "unverified") {
      return (
        <Notice tone="bad" id="unverified" title="The extraction store was migrated but failed verification afterwards.">
          <p className="mt-0.5">Do not rely on it. An operator should restore the backup that the migration kept, or repair the store, and check it again.</p>
        </Notice>
      )
    }

    // The correction-store warning already says this whenever corrections cannot be read; do not say it twice
    if (staleness.data?.corrections_available === false) return null

    return (
      <Notice tone="bad" id="unavailable">
        <p>The extraction store cannot be used right now. Ask an administrator to check it.</p>
      </Notice>
    )
  }

  return (
    <Notice tone="warn" id="unknown">
      <p>The server reported an extraction store state this page does not recognise. Extraction writes are disabled until an operator checks it.</p>
    </Notice>
  )
}
