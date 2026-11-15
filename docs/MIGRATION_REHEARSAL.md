# Migration rehearsal

The schema upgrades the real data needs, rehearsed on copies of its first verified backup, never on the data
itself. Sprint 4, phase 1, 2026-11-15.

## 1. What the real data needs

| Database | Real data | Current code | Upgrade |
|---|---|---|---|
| Exception reviews (`audit_reviews.sqlite3`) | version 2 | version 3 | 2 → 3: the review table is rebuilt so it also allows the `disputed` and `waived` states (each needing its reason) |
| Audit history (`audit_history.sqlite3`) | version 3 | version 6 | 3 → 4: a new table, `freight_audit_records` (append-only); 4 → 5: two indexes; 5 → 6: a new column, `run_inputs.tms` |
| Ingestion register (`ingestion_register.sqlite3`) | version 1 | version 1 | none |

The other stores (sign-in, extractions, carrier registry, integrations) do not exist in the real data: they are
created at their current version when first used, with nothing to upgrade.

**When an upgrade runs:**

- **Reviews:** only when a change opens the store: an exception action, or the server's background loop recording
  newly detected exceptions. Since this phase a read never opens it for writing, so a read never upgrades it
  (section 5).
- **History:** whenever the history store is opened, which includes reading the history.

Either way, the first start of the new code on the real data folder upgrades both databases.

## 2. How it was rehearsed

```
python scripts/rehearse_migration.py --source <restored>/data --chroma <restored>/chroma_db --work <new folder>
```

**Steps:**

1. The source is the backup made and verified in phase 0, restored with `manage_backups.py restore` into a
   separate folder; the script only reads it.
2. The script copies it into a new folder, refusing the installation's own data folder, index and databases as
   source or destination.
3. It records the facts of every database, read-only.
4. It opens the copies with the application's own store classes, exactly as the server does, so the upgrades run.
5. It checks the result.

Its report (`rehearsal-report.json` in the work folder) holds versions, row counts, object names and digests,
never a row's content.

It ran three times on fresh copies: once with an earlier draft of the script, then on Python 3.12 and on
Python 3.10. The first run failed two checks:

- **The `schema_info` row changed.** That table holds the version number, which the upgrade changes on purpose,
  so the row check no longer compares it; the version is checked on its own.
- **`/api/history/compare` answered 422.** It needs parameters, so the script no longer calls it bare.

The two later runs passed every check.

## 3. Results (Python 3.12 and 3.10, identical)

| Check | Reviews | History | Register |
|---|---|---|---|
| Version before → after | 2 → 3 | 3 → 6 | 1 → 1 |
| Every earlier table, column and row kept (SHA-256 of all rows over the earlier columns) | yes: 10 reviews, 10 events | yes: 1 run, 4 snapshots, 4 findings, 4 input sets, 20 inputs, 4 run links | yes: 11 files, 33 events, 1 action |
| Added | — | table `freight_audit_records` (0 rows: earlier snapshots have none, never rebuilt), column `run_inputs.tms` (empty for earlier inputs), 2 indexes, 2 append-only triggers | — |
| No index or trigger lost (`review_events` stays append-only) | yes | yes | yes |
| `PRAGMA integrity_check` / `foreign_key_check` | ok / 0 | ok / 0 | ok / 0 |
| Review history hash chain (`verify_integrity`) | 10 events checked, 0 problems | — | — |
| Copy kept before the review upgrade (`audit_reviews.before-v3.sqlite3`) | at version 2, identical rows | — | — |
| Opening the upgraded databases again changes nothing | yes | yes | — |
| Every recorded snapshot reads back through the history store | — | 4 of 4 | — |

**The application on the upgraded copy** (local mode, nothing started in the background) answered 200 to every
read endpoint the script calls:

- health, overview, shipments, exceptions and their activity, carriers, completeness, exposure, documents;
- history runs, trends, re-audits;
- risk overview, table and trends;
- the report catalogue and all four report previews;
- the exceptions CSV and the ingestion register;
- for a shipment: its detail, risk explanation, re-audit preview, freight audit, TMS view and history.

**The source was not changed:**

- the restored databases are byte-identical to the backup's;
- the backup still matches its manifest (23 of 23 files);
- the live data's 34 files are unchanged (`sha256sum -c`, before and after every run).

## 4. What cannot be undone, and how to roll back

**Forward-only.** No upgrade has a down-migration, and each store refuses a database newer than the code that
opens it ("schema version N, expected M"). So once the real data is upgraded, an older build of LogiDoc-RAG
cannot open it.

**Irreversible in place:**

- **Reviews 2 → 3 rebuilds the review table:** a new table is created, every row copied, the old table dropped.
  The rows are identical (checked above), but the old table no longer exists. Before it, the store writes a
  whole copy of the database beside it (`audit_reviews.before-v3.sqlite3`); that copy is the in-place way back.
- **History 3 → 6 is additive** (a table, indexes, a nullable column): earlier rows are not touched. Freight audit
  records and TMS columns written after the upgrade have no place in a version 3 database.

**Rolling back means restoring the backup made before the upgrade:**

1. Stop the server.
2. Restore the backup: `python scripts/manage_backups.py restore <backup> <new folder>`, then point
   `LOGIDOC_DATA_DIR` and `LOGIDOC_CHROMA_DIR` at it, or move it into place. Do not copy single files over a live
   folder.
3. Start the earlier build.

Everything recorded after the upgrade is lost: reviews and their events, recorded runs, freight audit records,
corrections and TMS decisions. For the reviews alone, the `.before-v3` copy can replace the upgraded file, with
the same loss.

**Before the production upgrade:**

- make a new backup and verify it (phase 0's backup is the state of 2026-11-15 only);
- rehearse again on a restore of that backup with the release candidate;
- keep the backup until the upgraded installation has been checked.

The upgrade itself is a separate, approved step: it was not run on the real data.

## 5. Reads never upgrade

Before this phase, reading the exceptions (and anything built on them: carriers, risk, reports, the dashboard)
opened the review store for writing, which created or upgraded it. A read now copies it into memory through a
read-only connection (`review_store.ReviewView`). `tests/test_read_only_requests.py` calls every GET operation on
a version 2 database and checks:

- the file comes out byte for byte;
- it stays at version 2;
- no `.before-v3` copy is made.

The history store still upgrades when opened, and a history read opens it.
