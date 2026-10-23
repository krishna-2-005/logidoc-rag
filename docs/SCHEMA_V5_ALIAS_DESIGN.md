# Extraction store schema v5: alias migration safety audit and design

> **Implementation status (2026-10-23).** Step 0 (store states, read-only GET) and schema 5 are implemented:
> `src/extraction_store.py` (states, openers, `extraction_aliases`, `save_with_alias`), `src/extraction_migration.py`
> (verified backup, history verification with no override, one transaction, baseline comparison) and
> `scripts/migrate_extraction_store.py`. Requests never upgrade a schema, including from versions below 4.
> Decisions as built: D1 explicit command only; D2 conflict recorded then 409; D3 aliases do not reach the audit yet;
> D4 failed verification blocks, no override; D5 the operator chooses the backup folder and keeps the backups, nothing
> is deleted by the tool; D6 aliases are not hash-chained (append-only triggers, relation and hash checks, and
> `verify_history`). Tests: `tests/test_store_states.py`, `tests/test_store_migration.py`. The text below is the design
> that was reviewed; where it differs (the migration also verifies the backup copy and compares the baseline under
> the lock), the code is authoritative.

Status of the original audit: **design only, nothing implemented**. No database was modified. Everything below about behaviour was
read from the code or shown by a probe against a temporary file. All fixtures proposed are synthetic.

## 1. Current schema and dependency map

`src/extraction_store.py`, `SCHEMA_VERSION = 4`, separate SQLite file (`Locations.extraction_path()`,
`<raw folder parent>/extractions.sqlite3`, `src/audit_service.py:78`).

| Table | Key / constraint | References | Protection |
|---|---|---|---|
| `schema_info` | one row, `version` | none | read/replaced inside each migration step |
| `extractions` | PK `id`; **`UNIQUE (sha256, extractor_version)`** | none | triggers: no UPDATE, no DELETE |
| `field_corrections` | PK `id` | `extraction_id -> extractions(id)` | triggers: append-only; hash chain (`prev_hash`, `hash`) over extraction_id, field, values, reason, actor, time, based_on |
| `correction_decisions` | PK `id`; `correction_id` UNIQUE | `correction_id -> field_corrections(id)` | triggers: append-only; hash chain; proposer cannot decide (checked in `decide` and `verify_history`) |

Facts that matter for aliases:

- `extractions.filename` is the name of the *first* upload only; the row is immutable (trigger).
- The hash chain covers `extraction_id`, **not** the filename, so adding alias rows cannot invalidate it.
- Readers that use the filename: `current_extractions` (matches `row["filename"]` to the folder's file hashes,
  `extraction_store.py:715`), `audit_inputs.build_overrides` (`hashes.get(correction["filename"])`,
  `audit_inputs.py:155`), `build_inputs` (`extractions.get(source_name)`). A second name for the same bytes
  therefore gets no cross-check row and, if the first name's file is gone, its approvals are ignored as
  `document_changed_or_removed`. Approvals themselves already apply by content: `approved_for_content` joins on
  `sha256 + document_type + field`.
- Uploads (each event, with name, hash, size, actor) are recorded in the **ingestion register**
  (`ingestion_files`, `ingestion_events`, `ingestion_actions`), a different database. The extraction store has no
  upload concept.
- Transactions: `write()` is `BEGIN IMMEDIATE` / commit / rollback; `initialize()` applies one migration step per
  transaction, re-reading `schema_info` under the write lock, and rolls back on any error. Existing tests prove
  rollback and retry for v1 to v2 and v3 to v4 (`test_extraction_store.py:222`,
  `test_correction_aware_audit.py:671`).
- Current alias behaviour (added in the last task, no schema change): `save()` raises `AliasTypeConflict` when
  the same content is already stored under another type; `/api/extractions/run` answers 409 and records nothing.

## 2. Risks found

| # | Risk | Where | Evidence |
|---|---|---|---|
| R1 | **Approvals disappear from the audit for a v4 file under v5 code.** `open_if_current` returns `None` unless the file is exactly `SCHEMA_VERSION`. `None` is also what "no file" and "too old to hold approvals" return, and `audit_service` treats `None` as "no store": token `(0,0,0)`, no overrides, `corrections_available` true, no warning. | `extraction_store.py:679-711`, `audit_service.py:118-133, 190-200` | Probe: v4 file with v4 code gives a store; with `SCHEMA_VERSION = 5` in memory gives `None`; missing file gives `None`; corrupt file raises `StoreUnavailable`. For v<4 this is correct (legacy corrections never apply). For v4 it silently drops real approvals. |
| R2 | The first API call that builds a store migrates the file, as a side effect of any read. `store_of(state)` calls `ExtractionStore(path)`, whose `__init__` runs `initialize()`, for GET list, detail, pending, integrity as well as writes. Until then the audit sees R1. A viewer's GET triggers an unbacked-up schema change. | `routes_extraction.py:79-86`, `extraction_store.py:__init__` | code reading |
| R3 | A run recorded in the R1 window is recorded without the approvals. The history is append-only, so it stays; after migration `staleness()` would flag it (latest run lacks the applied ids) and a recorded recalculated run supersedes it. Self-healing, but a wrong run exists. | `audit_service.record_corrected_run`, `audit_history.staleness` | code reading |
| R4 | A v4 binary opening a v5 file (rollback of the code) gets `StoreError` "newer" in `initialize()` and `StoreUnavailable` in `open_if_current`. That is loud and safe (the audit marks `correction_store_unavailable`), but the API returns 500s on every extraction route until the file is restored. | `initialize()`, `open_if_current` | code reading |
| R5 | `statements()` splits scripts on `";\n"`. A trigger body containing `;` followed by a newline would be cut in the middle. v5 triggers must keep `BEGIN ...; END;` on one line like v2-v4. | `extraction_store.py:statements` | code reading |
| R6 | `ExtractionStore(path)` is built per request and takes five `BEGIN IMMEDIATE` locks (one per target version) even when current. Not a correctness problem; it widens R2 and adds write-lock contention. | `initialize()` | code reading |
| R7 | Filename-keyed audit lookups ignore aliases (above). Not a migration risk, but it decides what the alias table must serve. | `audit_inputs.py`, `current_extractions` | code reading |
| R8 | `AliasTypeConflict` is raised inside `write()`, so a refused conflict leaves no trace at all; there is no record that a second name was ever submitted. | `extraction_store.save` | code reading |

## 3. Distinguishing the states (requirement of section 2)

`open_if_current` must stop returning one value for four different situations. Proposed result (a small enum
plus the store, never an exception for the first four):

| State | Meaning | Audit behaviour | API `/staleness` field `store_state` |
|---|---|---|---|
| `absent` | no file | no corrections exist: as today | `absent` |
| `no_approvals_possible` | version < 4 (approvals did not exist) | as today (legacy never applies) | `migration_pending` (the migrate command applies steps 1 to 5) |
| `migration_pending` | version 4, code expects 5 | **reads approvals normally** through queries that exist at v4; never touches alias tables; flags `migration_pending` | `migration_pending` |
| `current` | version 5 | normal | `current` |
| `unavailable` | unreadable, newer than the code, or v5 missing the alias table or its triggers (damaged or half-applied by an outside tool) | `StoreUnavailable`: `correction_store_unavailable` on every input, as today | `unavailable` |
| `migration_failed` (service state, not a file state) | the explicit migration ran and rolled back | file is still v4: audit keeps reading approvals; API write routes answer 503 with the reason; administrator sees it | `migration_failed` |

A database with no approved corrections is `current` or `migration_pending` with an empty list: it must not map to
any of the unavailable states. "No approvals" and "approvals unreadable" stay different answers.

## 4. Alias model

Aliases belong to a **content identity observed under an extractor version**, not to an upload. Uploads stay in the
ingestion register. Document type inferred from a file name depends on the classifier, which is part of the
extractor version, so the alias records the inference with its version.

```sql
CREATE TABLE IF NOT EXISTS extraction_aliases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id     INTEGER NOT NULL REFERENCES extractions (id),   -- the stored row this name was observed against
    sha256            TEXT    NOT NULL,                                 -- copy of extractions.sha256, checked by verify_history
    extractor_version TEXT    NOT NULL,                                 -- copy of extractions.extractor_version
    filename          TEXT    NOT NULL,
    inferred_type     TEXT    NOT NULL,                                 -- what this name (with the content) classified as
    relation          TEXT    NOT NULL CHECK (relation IN ('original', 'same_type', 'type_conflict')),
    first_seen_at     TEXT    NOT NULL,
    seen_by           TEXT,                                             -- actor of the first observation; NULL for backfilled rows
    UNIQUE (sha256, extractor_version, filename)
);
CREATE INDEX IF NOT EXISTS aliases_by_extraction ON extraction_aliases (extraction_id);
CREATE INDEX IF NOT EXISTS aliases_by_name ON extraction_aliases (filename);
-- append-only triggers, same one-line form as v2 to v4
```

Rules:

- The existing `UNIQUE (sha256, extractor_version)` on `extractions` is **not changed**. No table rebuild.
- `original`: the name the extraction row was made from. Backfilled for every existing row.
- `same_type`: another name, same inferred type: the extraction row is reused, an alias row is added.
- `type_conflict`: another name, different inferred type. The alias is **recorded** and the extraction row, its
  type, its corrections and approvals are untouched. A conflict alias never feeds the audit and never gains
  approvals. The type of the stored row can only change by extracting under a new extractor version.
- Duplicate submission (same content, version, name): no new row, result `created: false`, relation of the existing
  row returned. Concurrent submissions serialize on `BEGIN IMMEDIATE`; the UNIQUE key is the backstop
  (`INSERT OR IGNORE`, then select).
- Provenance: `first_seen_at` (UTC, same format as `now_text()`), `seen_by` from the authenticated principal,
  and a link to the extraction row. Not hash-chained (observations, not decisions); append-only triggers plus a
  `verify_history` check that each alias row's `sha256` and `extractor_version` equal its extraction row's.
- The conflict observation is committed in its own transaction **before** `AliasTypeConflict` is raised (R8).

API (no breaking change):

- `POST /api/extractions/run`: response gains `alias: {filename, relation, created}`. A conflict keeps answering
  409 (decision D2), with the body naming the existing id, existing type and the new type.
- `GET /api/extractions/{id}`: gains `aliases: [{filename, relation, inferred_type, first_seen_at}]`.
- List summaries gain `alias_count` and `has_type_conflict`.
- `GET /api/extractions/staleness`: gains `store_state` (section 3). Existing fields unchanged.

Audit (a separate step after the migration, behind its own tests, decision D3): `current_extractions` also matches
`same_type` alias names so a second name gets its cross-check row; `build_overrides` considers a correction valid
when any file in the folder has the correction's `sha256` under a compatible name.

## 5. Safe migration sequence

**Step 0, ship first, on its own, before any v5 schema exists.** Replace the single return of `open_if_current` by
the states in section 3 and make the audit read approvals from `migration_pending`. Ship the explicit migrate
entry point. This makes the rollout order safe in both directions: the new reader works on a v4 file, so the
file can be migrated at a time of the administrator's choosing. Without this, deploying v5 code is R1.

**Step 1, preconditions (the migrate command, read-only until step 3).**
1. Refuse if `schema_info` is not exactly 4 (3 or lower: run the existing steps first; 5: nothing to do; above 5 or unreadable: stop).
2. `PRAGMA integrity_check` and `verify_history()` must pass. A failing history is reported and the migration does not start (decision D4: whether to allow an override).
3. Back up with the SQLite online backup API to `extractions.sqlite3.pre-v5-<UTC time>`; verify the copy opens and has the same row counts. The backup is never deleted by the tool.
4. Record counts and maxima: rows of `extractions`, `field_corrections`, `correction_decisions`, their max ids, the ids of approved corrections, the chain tip hashes.

**Step 2, one transaction (`BEGIN IMMEDIATE`, `MIGRATIONS[5]`)**, idempotent in every statement:
1. `CREATE TABLE IF NOT EXISTS extraction_aliases`, indexes and triggers `IF NOT EXISTS` (one-line trigger bodies, R5).
2. Backfill: `INSERT OR IGNORE INTO extraction_aliases (...) SELECT id, sha256, extractor_version, filename, document_type, 'original', created_at, NULL FROM extractions`. Insert-only into the new table; nothing existing is read for write, updated or deleted (and the triggers would refuse it).
3. Inside the transaction, compare to step 1.4: counts and maxima of the three old tables unchanged; alias rows equal extraction rows. Any difference raises, which rolls back.
4. `DELETE FROM schema_info; INSERT ... 5` as the last statement, then commit.

Because the version row changes in the same transaction as the table, there is no committed state "version 5 without the alias table" and no state "alias table with version 4 that is half filled". Retry after a failure is the same command; a second process that arrives meanwhile blocks on the write lock, re-reads the version, and skips.

**Step 3, after commit.** Recompute `verify_history()`; compare the approved correction ids and chain tips to step 1.4; the audit's `corrections_token` is unchanged by the migration (approvals count, max decision id and max extraction id are the same), so no spurious cache invalidation and no spurious recomputation. Recorded audit runs (`audit_history.sqlite3`) are a different file and are not opened by the migration.

**After a failed migration**: the file is still v4, readable (step 0), approvals still apply, the API write routes answer 503 with the failure reason, `store_state = migration_failed` is shown, the backup exists, and the administrator re-runs the command. Restoring the backup is only needed if integrity checks fail after a commit.

**R2 fix**: reads (`store_of` for GET routes) must open the store without migrating (read-only open like `open_if_current`); only the migrate command, the startup hook (if decision D1 chooses it) and write routes may migrate. `decide` in `audit_service` keeps requiring a current store and says "migration pending" otherwise.

**What stays preserved, and how**

| Item | How |
|---|---|
| Approved, pending, rejected corrections | no row of `field_corrections` or `correction_decisions` is touched; counts and ids compared inside the transaction |
| Hash chains | no hashed column changes; `verify_history()` before and after |
| Extraction rows and ids | not updated (trigger), ids unchanged, `UNIQUE (sha256, extractor_version)` kept |
| Recorded runs and snapshots | other database, not opened |
| Provenance | `original` alias row records the first filename and time; payload hashes unchanged |
| Reviewer separation, permissions | unchanged code paths (`decide`, `authz`); aliases create no approvals and need no new permission beyond `extraction.run` for the run route |

## 6. Regression-test matrix (synthetic fixtures only)

| # | Case | Expected |
|---|---|---|
| 1 | Fresh database | version 5, alias table and triggers exist, `verify_history` ok |
| 2 | v4 file with an approved correction | migrate; approval still approved, `approved_corrections` equal before and after; audit uses it; token unchanged |
| 3 | v4 with pending and rejected corrections | statuses and decisions unchanged; pending still has no effect |
| 4 | v3 file (legacy correction) | goes 4 then 5; legacy still never applied |
| 5 | `verify_history` before and after | ok both times; chain tips identical |
| 6 | Backfill | one `original` alias per extraction row, names and times equal the rows |
| 7 | Same content, same filename | no new alias row; `created: false` |
| 8 | Same content, other filename, same type | one `same_type` alias; extraction row reused; ids unchanged |
| 9 | Same content, other filename, other type | `type_conflict` alias recorded; 409; the stored row's type unchanged; no approvals on the alias; audit unaffected |
| 10 | Duplicate alias submissions | one row |
| 11 | Concurrent alias submissions (two threads/processes) | one row, no error leaks, both answers consistent |
| 12 | Concurrent migration (two processes) | applied once, both succeed |
| 13 | Interrupted migration (failure injected after the backfill, before the version row) | still v4, no alias table, counts unchanged; retry succeeds |
| 14 | Failure injected in the verification step | rollback; same as 13 |
| 15 | Half-applied file made by hand (v4 with the table present) | migrate completes idempotently; `v5 without table` is reported `unavailable` |
| 16 | v4 file opened by the v5 audit | approvals read, `store_state = migration_pending`; never "no store" (regression test for R1) |
| 17 | Missing file / no approvals / corrupt file / newer version | four different states and messages |
| 18 | GET routes on a v4 file | do not migrate it (R2) |
| 19 | Migration failed, then write route | 503 with reason; GET and audit still work |
| 20 | v5 file opened by v4-era code (simulated by `SCHEMA_VERSION = 4`) | `StoreUnavailable`, visible warning, no silent loss |
| 21 | Extractor version change after migration | new extraction row, new alias rows for the same names; old rows and aliases kept |
| 22 | Existing suites | `test_extraction_store`, `test_extraction_audit_fixes`, `test_correction_aware_audit`, `test_extraction_e2e`, `test_api`, `test_parser_integrity_gaps` unchanged except the version number |
| 23 | Recorded runs | `audit_history` file bytes identical before and after (hash) |
| 24 | Audit by alias (step after migration) | second name gets its cross-check; approval valid when only the second name remains |

Every fixture is generated in a temporary directory; none uses a production or user database.

## 7. Decisions for the project owner

- **D1.** Who runs the migration: explicit command only, or also at API start-up. Recommended: explicit command, with start-up refusing writes and showing `migration_pending` until it is done.
- **D2.** A type-conflicting alias: recorded and 409 (recommended, current client behaviour kept), or recorded and 200 with a conflict marker.
- **D3.** Whether an alias name may serve the audit (cross-check and approval validity). Recommended: yes for `same_type` only, as a separate change after the migration.
- **D4.** Whether a failing `verify_history` blocks the migration (recommended) or can be overridden by an administrator with a recorded reason.
- **D5.** Backup retention and location (the tool creates it and never deletes it).
- **D6.** Whether aliases are hash-chained (recommended: no; append-only plus a consistency check).

## 8. Verdict

**READY WITH CONDITIONS.** The v4 to v5 migration itself is small and low risk: additive table, insert-only backfill,
one transaction, the same retry mechanism already tested twice. The real hazard is not the migration but R1/R2:
under the current `open_if_current`, deploying v5 code makes every v4 approval invisible to the audit without any
warning. Conditions before implementation: (1) step 0 ships first and its regression test (matrix 16, 17, 18) passes;
(2) the migrate command with backup and in-transaction invariants exists; (3) decisions D1 to D4 are made.
Not claimed: anything about real documents; how often identical content arrives under two names is unknown.

## 9. Durable migration-failure reporting (implemented: see docs/MIGRATION_STATUS_DESIGN.md)

Finding (2026-10-23, from the code): the API cannot reliably tell "a migration was attempted and failed" from "no
migration has been attempted". `migration_failed` comes from `extraction_store.MIGRATION_FAILURES`, a dictionary in the
memory of the process that ran `extraction_migration.migrate()`. The command-line tool runs in its own process, so a
failed run there leaves the file unchanged (by design) and the server keeps reporting `migration_pending`. The web
interface therefore states only what the server reports (`store.state`), says that a state of "pending" cannot show
whether an attempt was made elsewhere, and never displays the server's reason text (for a failed attempt it names an
exception class).

Smallest safe design, if durable reporting is wanted (needs review before it is built): the migration command writes a
small status file next to the store (`<store>.migration-status.json`: UTC time, `outcome` of `failed` or `succeeded`, a
fixed reason code such as `verification_failed`, `backup_failed` or `rolled_back`, and the backup file name), replacing
the previous one; `ExtractionStore.inspect` reads it and reports `migration_failed` with that code until a later run
succeeds. It cannot live inside the database: a failed migration is rolled back, so nothing written there would persist.
Open questions for the owner: who may read it, whether the code is shown to non-administrators, and whether a stale
file (older than the store) should be ignored.

> Update: the design above was superseded by `docs/MIGRATION_STATUS_DESIGN.md`, which keeps the status file next to the store but binds
> it to the store's history (not only its name), reconciles it with the schema, and records start, failure code, success and
> committed-but-unverified.
