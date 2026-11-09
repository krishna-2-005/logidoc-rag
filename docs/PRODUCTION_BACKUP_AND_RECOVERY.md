# Production backup and recovery

How LogiDoc-RAG backs up its data, what a backup contains, how it is verified,
and how it is restored. Day-to-day commands, the schedule and retention are in
[AUTOMATED_BACKUP_OPERATIONS.md](AUTOMATED_BACKUP_OPERATIONS.md).

Code: `src/backup.py` (backup, verification, restore), `src/file_lock.py`
(cross-process locks), `scripts/manage_backups.py` (command line).
Tests: `tests/test_backup_recovery.py`, `tests/test_backup_automation.py`.

## What has to be protected

| Store | Default location | What it holds | Schema version (this code) |
|---|---|---|---|
| Exception reviews | `data/audit_reviews.sqlite3` | review status, notes, assignments, review history | 3 |
| Audit history | `data/audit_history.sqlite3` | append-only audit runs, shipment snapshots, freight audit records | 5 |
| Users and sessions | `data/auth.sqlite3` | accounts, password hashes, session token hashes, sign-in events | 1 |
| Ingestion register | `data/ingestion_register.sqlite3` | per-upload status and the processing error register | 1 |
| Extractions | `data/extractions.sqlite3` | extracted fields, proposed and approved corrections | 5 |
| Carrier registry | `data/carrier_registry.sqlite3` | registered carriers, identifiers, approved name links | 1 |
| Documents | `data/raw/` | the PDFs and the ingestion journal (`.ingestion_journal.jsonl`) | - |
| Archived originals | `data/archive/` | originals an administrator replaced or removed | - |
| Retry copies | `data/quarantine/` | copies of uploads kept for a retry | - |
| Vector index | `chroma_db/` | ChromaDB: `chroma.sqlite3` and the HNSW segment files | ChromaDB 1.5.9 |

Every SQLite store records its version in a `schema_info` table, which the
backup reads. Each location can be moved with its setting (`LOGIDOC_DATA_DIR`,
`LOGIDOC_CHROMA_DIR`, `LOGIDOC_REVIEW_DB` and the others in `.env.example`).

Shipments are not stored separately. A shipment is rebuilt from its documents
on every audit, so restoring the PDFs together with the databases restores
every document-to-shipment relationship, every correction and every recorded
result.

Generated reports (CSV and PDF exports, `reports/`) are produced on request
from the stores and are not backed up. The frozen evaluation datasets live in
the repository and are covered by Git, not by these backups.

## How a backup is made

1. **One at a time.** A backup takes `<backups>/.backup.lock`, a lock the
   operating system releases if the process dies, so a crash never leaves a
   stale lock. A second backup in the same folder, from any process, is
   refused as busy.
2. **Space first.** The size is estimated from the sources. Unless the free
   space covers 1.2 times that plus `LOGIDOC_BACKUP_MIN_FREE_MB` (1024 by
   default), the backup stops before writing anything. Nothing is ever
   deleted to make room. On Windows, the backup also stops before writing
   if any file it would write has a path longer than 259 characters. The
   SQLite in Python 3.10 (3.40) cannot open such a file, so keep
   `LOGIDOC_BACKUP_DIR` and restore targets short, for example
   `D:\backups\logidoc`. Restores apply the same check to their target.
3. **Quiet copy.** Documents, databases and the vector index are copied while
   nothing can write to them (see the next section).
4. **Databases.** Each store is copied with SQLite's online backup API
   (`Connection.backup`) from a read-only connection. That gives a consistent
   snapshot even while the application writes: a transaction still open at
   that moment is not in the copy, and committed ones are. The test
   `test_uncommitted_changes_are_not_in_the_backup_and_committed_ones_are`
   shows it. The copy is checked with `PRAGMA integrity_check`, and its schema
   version, the row count of every table and `PRAGMA foreign_key_check` are
   recorded.
5. **Files.** Each file is copied while its SHA-256 is computed, then the
   source is hashed again. A file that changed during the copy is copied
   again, up to three times, and otherwise the backup fails as
   `source_changed`.
6. **Verification.** Every file in the backup is read back and compared with
   the size and checksum recorded for it.
7. **Completion.** The manifest is marked `complete`, its own checksum is
   written beside it (`manifest.sha256`), and the folder is renamed from
   `<id>.partial` to `<id>`.

A backup is usable only when its folder has no `.partial` suffix, its manifest
matches `manifest.sha256`, its status is `complete`, and every file matches
its checksum. An interrupted backup (power cut, Ctrl+C, a killed process)
stays `<id>.partial` with status `in_progress` or `failed`. It is listed as
incomplete and cannot be restored or activated.

## ChromaDB

ChromaDB runs embedded (`PersistentClient`). Its folder holds `chroma.sqlite3`
(collections, metadata and the write log) and HNSW segment files that ChromaDB
flushes from memory at intervals. Copying these files while ChromaDB writes is
not safe: a flush between copying the segment files and copying the database
can leave a copy whose log and segments disagree.

The backup therefore copies the index only while it is quiet:

- **Backups made by the server** (the schedule, `POST /api/admin/backups`)
  hold the ingestion lock (`ingestion_manager.INGESTION_LOCK`) for the whole
  copy. Every upload, replacement, removal and reindex takes that lock, so
  none can run during the copy. Uploads wait until the backup ends; for
  today's data that takes about a second.
- **Backups from the command line** take the server's data-folder lock
  (`.<data folder name>-server.lock`, beside the data folder). The server
  holds this lock while it runs, so the command can only take it when the
  server is stopped. While the server runs, the command still copies the
  databases and documents consistently, but leaves the index out. The
  manifest says so, and `--require-index` turns that into a failure.

`chroma.sqlite3` is copied with the SQLite backup API and the segment files
byte for byte. The backup also records, from the copy, the number of vectors,
the collections, the documents indexed (source name and SHA-256 with their
chunk counts), and how the index compares with the documents. These are read
with SQL, never by opening the copy with ChromaDB, which would change it.

The vector index is derived data. A backup without it is still a complete
recovery point: rebuild the index from the restored documents with
`python src/vector_store.py`, with `LOGIDOC_CHROMA_DIR` pointing at the
restored folder.

The Streamlit application (`app.py`) is a separate process and does not take
the server's lock. As before, do not upload or change documents from
Streamlit while the API server runs on the same data folder, and stop
Streamlit before a backup from the command line.

## The manifest

`manifest.json` in each backup folder, written with sorted keys:

```json
{
  "format": "logidoc-backup",
  "format_version": 1,
  "backup_id": "logidoc-20261109T023000Z-scheduled-a1b2c3",
  "origin": "scheduled",
  "label": null,
  "status": "complete",
  "created_at": "2026-11-09T02:30:00Z",
  "completed_at": "2026-11-09T02:30:02Z",
  "application": {"git_commit": "068074e...", "python": "3.12.x", "chromadb": "1.5.9"},
  "components": {
    "databases": {"included": true, "items": {"audit_history": {
      "present": true, "file": "databases/audit_history.sqlite3", "size": 73728, "sha256": "...",
      "integrity": "ok", "schema_version": 5, "tables": {"audit_runs": 2}, "foreign_key_problems": 0}}},
    "documents": {"included": true, "folders": {"raw": 13, "archive": 0, "quarantine": 0}, "pdfs": 12},
    "vector_index": {"included": true, "present": true, "chromadb_version": "1.5.9", "readable": true,
      "collections": ["logistics_documents"], "embeddings": 40,
      "indexed_documents": {"BOL_SHJ-2026-001.pdf": {"<sha256>": 3}},
      "consistency": {"documents_not_indexed": [], "vectors_without_document": []}},
    "configuration": {"included": true, "file": "configuration.json"}
  },
  "files": [{"path": "files/raw/BOL_SHJ-2026-001.pdf", "component": "documents", "size": 2210, "sha256": "..."}],
  "totals": {"files": 21, "bytes": 1048576},
  "restore_compatibility": {"format_version": 1, "schema_versions": {"audit_history": 5},
    "rule": "Restorable by an application whose stores are at these schema versions or newer ...",
    "chromadb_version": "1.5.9"},
  "error": null
}
```

The example is abridged: the real manifest lists every store, every table
and every file.

- `origin` is `manual` (the command line, or an administrator through the
  API) or `scheduled`. Retention only ever removes `scheduled` backups.
- `configuration.json` records the settings that change results: sign-in
  mode, review policy, upload limit, freight tolerances, models, collection
  and chunking. It holds no paths and no secrets.
- `application.git_commit` is read from `.git` without running Git, and is
  `null` outside a Git checkout.
- A failed backup's manifest has `status: "failed"` and an `error` with a
  fixed `category` (`insufficient_space`, `path_too_long`, `source_changed`,
  `verification_failed`, `storage_error`, `database_error`, `busy`,
  `interrupted`, `unexpected_error`) and a message with paths and secrets
  removed.

## Restoring

```
python scripts/manage_backups.py restore <backup id> --target <new folder>
```

A restore always goes to a new or empty folder. It refuses:

- the live data folder
- the vector index folder
- the backup folder
- the project folder
- any folder inside one of those, or one that contains one of them

Its steps:

1. **Manifest.** It parses, matches its checksum, is of a known format, and
   its status is `complete`.
2. **Checksums.** Every file is present with its recorded size and SHA-256,
   and every database copy passes its integrity check.
3. **Compatibility.** A store whose schema version is newer than this
   application's is refused: restore it with the application version that
   made the backup. An older store is restored as it is, and the report says
   how it is upgraded. The application upgrades its stores when it opens
   them; the extraction store is upgraded with
   `python scripts/migrate_extraction_store.py`, because the API refuses an
   outdated one. A different ChromaDB version is noted, since ChromaDB may
   upgrade the index when it opens it.
4. **Copy.** Files go to `<target>/data/` (the default file names) and
   `<target>/chroma_db/`, each compared with its checksum after copying.
5. **Databases.** Each restored store has its integrity, schema version, row
   count of every table and foreign-key state checked against what was
   recorded at backup time. The audit history's runs, snapshots and freight
   audit records are reported.
6. **Documents and index.** The restored index is opened with ChromaDB. It
   must hold the recorded number of vectors and the same indexed documents,
   and its consistency with the restored PDFs must be what it was at backup
   time. Inconsistencies that already existed at backup time are warnings,
   not failures.
7. **Report.** `<target>/restore-report.json` with status `restored` or
   `failed`, every step, the problems and the warnings. While the restore
   runs, `<target>/RESTORE_IN_PROGRESS` exists; it is removed only when the
   status is `restored`.

To use a restore, start the application with
`LOGIDOC_DATA_DIR=<target>/data` and `LOGIDOC_CHROMA_DIR=<target>/chroma_db`.
Unset the per-store settings such as `LOGIDOC_REVIEW_DB` if they are set.
Nothing is copied over the live data: switching back means starting with the
old settings again.

### Replacing the live data

Only when the restored folder must take the place of the live one:

```
python scripts/manage_backups.py activate --restored <target> --backup-id <id> --confirm <id>
```

This is refused unless all of the following hold:

- the backup ID is typed twice
- the restore report says `restored` for that backup and no
  `RESTORE_IN_PROGRESS` marker is left
- the API server is stopped (the data-folder lock is free, and is held during
  the swap)
- every store is in the data folder under its default name
- the live data is not the project's own `data/` folder

The live data and index folders are renamed to
`<name>.pre-restore-<time>`, never deleted, and the restored ones are renamed
into their places. If a rename fails, everything already moved is moved back.
The restore must be on the same drive as the live data.

## Recovery validation

`tests/test_backup_recovery.py` builds a synthetic installation in a temporary folder
through the real application:

- the sample shipment PDFs and a contracted shipment
- an upload indexed with deterministic stand-in embeddings
- a recorded audit history run
- an approved extraction correction
- a disputed exception
- a registered carrier

On that installation it backs up, restores into a separate folder, and checks:

- every restored database is byte for byte the backed-up copy, and every PDF
  equals the live one
- integrity and foreign keys of every restored store
- the audit history's runs read back identically
- the restored index opens with ChromaDB and holds the backed-up vectors
- an API started on the restored folder returns the same shipment history,
  exception history and carrier registry as the original
- interrupted backups, a source that changes during the copy, and changed,
  missing or truncated backup files are detected, and such backups are never
  restored
- edited or unreadable manifests are refused, and so are targets inside live
  or backup folders and folders that already hold files
- a backup from a newer application is refused
- an older (version 1) audit history is restored as it was and upgraded to
  version 5 when the application opens it
- live data is replaced only with confirmation and with the server stopped,
  and is moved aside, never deleted

The real `data/`, `chroma_db/` and databases are never used as fixtures.
`tests/conftest.py` fails the run if they change.

## What this does not cover

- **Off-machine copies.** Backups are written to one folder. In production it
  must be on another disk than the data (`LOGIDOC_BACKUP_DIR`), and should be
  copied off the machine. Copying elsewhere (rsync, object storage, tape) is
  not built in.
- **Encryption.** Backups are not encrypted. They contain password hashes and
  business documents, so the backup folder must be readable by the service
  account only, and copies must be encrypted where they go.
- **Point-in-time recovery.** A restore returns to the moment of a backup.
  Changes made after it are not replayed.
- **Streamlit.** It is a second writer that does not take the server's lock.
  Do not run it on the same data as the API server during a backup from the
  command line.
- **No real recovery yet.** No restore has been performed on a real
  production installation. Only synthetic installations were restored in the
  tests.
