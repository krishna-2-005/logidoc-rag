# Durable migration status: design and threat review

Status: written before the implementation (Phase 1), updated with the as-built notes at the end. Reference:
`docs/SCHEMA_V5_ALIAS_DESIGN.md` §9, `src/extraction_migration.py`, `src/extraction_store.py`.

## 1. The problem, from the code

`migration_failed` used to come from `extraction_store.MIGRATION_FAILURES`, a dictionary in the memory of the process
that ran the migration. The command-line tool is a separate process, and a failed run leaves the database unchanged by
design, so a server could not tell "attempted and failed" from "never attempted". Nothing else records an attempt: the
database cannot hold it (a failed migration is rolled back, so nothing written inside it survives).

## 2. Principle

**The database is authoritative.** The schema version, the structural checks and, where a record claims a problem
after a commit, the integrity and history verification decide the state. A status record can add information about a
store that is in an older state; it can never make a store look better than it is, and it can never override the
schema version. A record that is missing, malformed, unreadable, from another store or out of date is ignored and the
state falls back to what the database says.

## 3. The record

- **Location and ownership.** `<store file>.migration-status.json`, in the same directory as the store, written only by
  the migration command (the operator's account). The server only reads it and needs read access only. Same directory
  means the same filesystem and the same access rules as the store; there is no second place to configure.
- **Format.** One JSON object, at most 8 KiB, format 1:

  | Field | Meaning |
  |---|---|
  | `format` | `1`; any other value is unreadable |
  | `attempt_id` | random id of the attempt that wrote it |
  | `store_name` | the store's file name (not its path) |
  | `outcome` | `started`, `failed`, `succeeded` or `committed_unverified` |
  | `code` | a fixed code (below) or null |
  | `from_version`, `to_version` | schema versions of the attempt |
  | `baseline` | SHA-256 fingerprint of the history as the attempt found it (counts, ids, content digests of the history tables, correction and approval ids, chain tips) |
  | `at` | UTC time |
  | `backup_name` | the backup's file name, kept in the record for the operator and never sent by the API |

  Codes: `history_verification_failed`, `integrity_check_failed`, `backup_failed`, `store_changed`, `rolled_back`,
  `verification_failed`, `post_verification_failed`. Nothing free-form is stored: no exception name, no message, no path.
- **Which database it belongs to.** Three checks, all must pass: the file sits next to this store and is named after
  it; `store_name` equals this store's file name; and, for a store still at an older schema, `from_version` equals the
  store's version and `baseline` equals the fingerprint of the store's history *now*. A record copied to another
  store, left behind by a restored or replaced database, or made stale by any change to the history, fails the
  fingerprint and is ignored.

## 4. States and how a record is reconciled with the schema

| Database says | Record says | Reported |
|---|---|---|
| absent, unavailable | anything | the database state; record ignored |
| older or pending (version below current) | `failed`, store and fingerprint match | `migration_failed` (confirmed), with the code |
| older or pending | `started`, store and fingerprint match | `migration_pending`, attempt `unfinished` (it may be running, or it was interrupted: it cannot be told apart, so it is not called a failure) |
| older or pending | `succeeded` / `committed_unverified` | ignored (the database was restored or replaced): pending |
| older or pending | `failed`/`started`, fingerprint or version differs | ignored (stale): pending |
| current | `succeeded`, to_version matches | `current`, attempt `succeeded` |
| current | `committed_unverified` | the integrity and history checks run again now: failing gives `unavailable` (code `post_verification_failed`), passing gives `current` and the record is ignored |
| current | `failed` or `started` | ignored (stale, another attempt won): `current` |
| any | missing | attempt `none`, record `missing` |
| any | malformed, unreadable, wrong format, oversized | attempt `none`, record `unreadable`: never a failure claim |

The API sends `store.migration = {attempt: none|failed|unfinished|succeeded, code, at, record: ok|missing|ignored|unreadable}`
and fixed reason sentences. No path, no exception name, no backup path.

## 5. Writing: atomic, crash-safe, honest

- Written to a temporary file in the same directory, flushed and `fsync`ed, then `os.replace`d over the record:
  readers see the old record or the new one, never a partial one. A crash before the replace leaves the old record and
  at worst a temporary file, which is ignored and never read.
- Order in the command: preflight (read-only) → validate the backup directory (operator errors are not attempts and
  write nothing) → **write `started`; if it cannot be written, stop before any change** → backup → transaction → on
  success post-migration verification → write `succeeded`.
- **Success is claimed only after the commit and a passing post-migration verification.** A commit followed by a failed
  verification writes `committed_unverified`, the command reports it as not verified and exits 1, and the store
  reports `unavailable` until the checks pass again. The backup is kept.
- A failure writes `failed` with its code. If that write itself fails, the command says so; the database was not
  changed and still reports pending, so no failure to record can make an invalid database look healthy.
- If the `succeeded` write fails after a verified commit, the command reports success with a warning; the schema says
  `current`, which is authoritative.

## 6. Concurrency

Two commands at once: both may write `started` (the later write wins); the database transaction serialises the actual
change under the write lock, and the loser finds the store current and applies nothing. Whatever the final record says,
reconciliation against the schema keeps the answer right: a `failed`/`started` record next to a current store is stale
and ignored; a `succeeded` record next to a current store is accurate. The record is advisory; it is never a lock.

## 7. Threats and decisions

| Risk | Decision |
|---|---|
| A status file from another database | store name plus history fingerprint (§3) |
| Forged or edited record claiming failure | can only show a failure notice for a store that is genuinely older; cannot change the schema state or make anything writable |
| Forged record claiming success | ignored unless the schema is current, and a `committed_unverified` claim only triggers re-verification |
| A record hiding a real problem | impossible: the schema and structural checks come first; a record can only add a notice |
| Permissions: unreadable or unwritable record | unreadable: ignored, no claim. Unwritable: the command stops before changing anything (start) or reports it (end); reading never needs write access |
| Malformed, truncated, huge or non-UTF-8 record | size cap, strict validation of every field and enum, any failure means unreadable |
| Disk full or error while writing | atomic replace: the old record stays; the command reports it |
| Path disclosure | the record holds file names only; the API sends fixed codes and sentences, never paths, exception names or the backup name |
| Stale temporary files after a crash | ignored by readers; left alone (nothing is deleted automatically) |
| Cost | the fingerprint reads the history tables, so it is computed only for a store still at an older schema that has a matching record |

## 8. Deferred / not decided

- No lock file or process-liveness check: an unfinished attempt is reported as unfinished, never as running.
- Backups are the operator's: nothing here deletes or rotates them.
- Whether non-administrators should see the failure code is a UI decision; the API sends only fixed codes.

## 9. As built (2026-10-23)

- `src/migration_status.py` (record, atomic write, strict validation, history fingerprint, reconciliation),
  `ExtractionStore.inspect` (reconciles with the schema; `migration` on the result), `src/extraction_migration.py` (writes
  `started`, then `failed` with a code, `succeeded` only after commit and the post-commit verification, or
  `committed_unverified`), `scripts/migrate_extraction_store.py` (prints the last attempt, warnings, NOT VERIFIED).
- The process-local `MIGRATION_FAILURES` registry is removed; `migration_failed` now only comes from a verified record.
- The API's `store` object gains `migration: {attempt, code, at, record}` and nothing else about the record. State
  reasons are fixed sentences. No path, exception name or backup name is sent.
- UI: the shared notice (`storeState.tsx`) calls a failure only when `attempt` is `failed` and `record` is `ok`, gives
  recovery guidance per fixed code, shows unfinished, unreadable, ignored and missing records as what they are, and shows
  `unverified` as its own state. No button; migration stays an operator command. The History page and the proposal dialog
  were not given the notice: the history page does not depend on the store's write state, and the proposal dialog can only
  be opened from a button that is already disabled while the store cannot take writes.
- Decision on the open question in §8: the API sends only the fixed code to any user who can see staleness.

Residual risks (facts, not fixed):
- If the post-commit verification fails and the `committed_unverified` record also cannot be written, the server cannot
  know and will report the store `current`; the command itself reports NOT VERIFIED and exits 1. The operator must act on
  that output.
- The record is bound to a history, not to a file identity: a byte-for-byte copy of the store, placed next to a copied
  record with the same file name, is indistinguishable and carries the record with it.
- Unfinished attempts cannot be told from running ones (no lock file or liveness check).
- Crash leftovers (`.<name>.migration-status.json.<id>.tmp`) accumulate until an operator removes them; nothing deletes them.
