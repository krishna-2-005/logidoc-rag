# Automated backup operations

Running backups day to day: the schedule, the commands, the status record,
retention, and the restore workflow. How a backup is built and verified is in
[PRODUCTION_BACKUP_AND_RECOVERY.md](PRODUCTION_BACKUP_AND_RECOVERY.md).

Code: `src/backup_automation.py` (schedule, runner, retention, status),
`src/operations.py` (the server's background jobs),
`src/api/routes_operations.py` (the administrators' endpoints),
`scripts/manage_backups.py` (command line).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_BACKUP_DIR` | `backups/` in the project (development) | where backups go; required in production, on another disk than the data, not inside the data or index folder |
| `LOGIDOC_BACKUP_SCHEDULE` | `off` | `off`, `daily@HH:MM` (UTC) or `every:<N>h`; required in production |
| `LOGIDOC_BACKUP_KEEP_DAILY` | 7 | days for which the newest scheduled backup is kept |
| `LOGIDOC_BACKUP_KEEP_WEEKLY` | 4 | ISO weeks for which the newest scheduled backup is kept |
| `LOGIDOC_BACKUP_KEEP_MONTHLY` | 6 | months for which the newest scheduled backup is kept |
| `LOGIDOC_BACKUP_MIN_FREE_MB` | 1024 | free space that must remain after a backup, or it does not start |
| `LOGIDOC_BACKUP_RETRY_MINUTES` | 60 | how long after a failed scheduled backup it is tried again |

## The schedule

The schedule runs inside the API server (`python src/api/serve.py`). A
background thread checks every 30 seconds whether a backup is due:

- **`daily@02:30`**: once per day, the first check after 02:30 UTC. A backup
  missed while the server was down runs as soon as it starts again.
- **`every:6h`**: when the last successful backup is 6 hours old.
- **After a failure**: the next try waits `LOGIDOC_BACKUP_RETRY_MINUTES`,
  never in a loop.

These backups hold the ingestion lock, so they always include the vector
index. Uploads wait for them. The jobs never start under `LOGIDOC_ENV=test`
or with `LOGIDOC_BACKGROUND_JOBS=0`.

To schedule from the operating system instead (cron, Task Scheduler), set
the schedule to `off` and run `scripts/manage_backups.py create`. Run it while
the server is stopped, or accept a backup without the vector index (see
below).

## Commands

All commands read the same settings as the server.

```
python scripts/manage_backups.py create [--label "before the upgrade"] [--require-index]
python scripts/manage_backups.py list
python scripts/manage_backups.py status
python scripts/manage_backups.py verify <backup id>
python scripts/manage_backups.py restore <backup id> --target <new folder> [--skip-index-open]
python scripts/manage_backups.py prune [--apply]
python scripts/manage_backups.py activate --restored <folder> --backup-id <id> --confirm <id>
```

Every command prints JSON and exits with 0 when it worked, 1 when it failed
or was refused, and 2 for bad arguments.

- **`create`** makes a manual backup. When the API server is stopped, the
  command holds its data-folder lock while it copies, so the backup includes
  the vector index. While the server runs, the index is left out (the
  databases and documents are still consistent) and the manifest says why;
  `--require-index` fails instead. For a full backup while the server runs,
  ask the server: `POST /api/admin/backups`.
- **`list`** shows every backup, newest first: usable ones, and incomplete
  `.partial` ones with their error.
- **`status`** shows the schedule, the retention, and the last attempt,
  success and failure.
- **`verify`** re-checks a backup: its manifest, every checksum and every
  database's integrity.
- **`restore`** restores into a new folder and validates it
  ([details](PRODUCTION_BACKUP_AND_RECOVERY.md#restoring)).
- **`prune`** previews retention; with `--apply` it removes what the preview
  listed.
- **`activate`** replaces the live data with a validated restore. The server
  must be stopped, the backup ID typed twice, and the live folders are
  renamed aside, never deleted.

## From the web API

Administrators (the `admin.maintenance` permission):

| Request | Result |
|---|---|
| `GET /api/admin/backups` | the schedule, retention, `running`, the status record and every backup (usable or incomplete) |
| `POST /api/admin/backups` with `{"label": "..."}` | 202: a backup starts in the background; 409 while one runs |
| `GET /api/admin/alerts` | active alerts, including `backup_failed` and `backup_stale` |

Restores are not offered over HTTP. They run on the server from the command
line, into a separate folder.

## Status and failures

After each attempt, `<backups>/backup-status.json` is rewritten atomically:

```json
{"last_attempt": {"at": "...", "outcome": "failed", "origin": "scheduled", "backup_id": null,
                  "category": "insufficient_space", "message": "Not enough free space ..."},
 "last_success": {"at": "...", "outcome": "success", "origin": "scheduled", "backup_id": "logidoc-..."},
 "last_failure": {"...": "..."}, "consecutive_failures": 1, "retry_minutes": 60}
```

When a backup fails:

- the attempt's folder stays `<id>.partial` with status `failed` and the
  error category, and is never treated as a backup
- the status record keeps the failure and counts consecutive failures
- the `backup_failed` alert fires (critical) through the log and the
  webhook, and clears with the next successful backup
- `logidoc_backup_runs_total{outcome="failed"}` counts it
- nothing is cleaned up

Two backups never run at once:

- within the server, a second request gets 409 and the scheduler skips its
  turn
- across processes, the backup folder's lock makes the second attempt fail
  as `busy`

## Retention

Retention runs after every successful backup, never after a failure. It
works only on scheduled backups:

1. It lists the usable scheduled backups, newest first.
2. It keeps the newest one of each of the last `KEEP_DAILY` days,
   `KEEP_WEEKLY` ISO weeks and `KEEP_MONTHLY` months, plus the newest usable
   backup of any origin.
3. It removes the other scheduled backups.

Before each removal it checks again that the folder:

- sits directly in the backup folder
- is not a link or junction
- has a valid manifest of origin `scheduled` and status `complete`

Otherwise the folder is skipped and reported.

Retention never removes:

- manual backups (command line or `POST /api/admin/backups`)
- incomplete (`.partial`) folders
- anything that is not a LogiDoc-RAG backup folder, or is outside the
  backup folder
- source documents or any live data

Nothing is removed to make room when the disk is short: the backup fails and
alerts instead.

Incomplete folders and manual backups are yours to remove, once you have
read their error.

## Restore runbook

1. **Pick the backup:** `python scripts/manage_backups.py list`. Choose a
   usable one, normally the newest.
2. **Verify it:** `python scripts/manage_backups.py verify <id>`. It must say
   `"ok": true`.
3. **Restore it:**
   `python scripts/manage_backups.py restore <id> --target D:\restore\<id>`,
   using a new folder on the same drive as the live data if you will activate
   it.
4. **Read the report.** `D:\restore\<id>\restore-report.json` must say
   `"status": "restored"`. Read the warnings: an index that was not in the
   backup, documents without vectors, stores that will be upgraded.
5. **Try it before switching.** Start a second server on the restore with
   `LOGIDOC_DATA_DIR=D:\restore\<id>\data`,
   `LOGIDOC_CHROMA_DIR=D:\restore\<id>\chroma_db` and another port. Check
   `/api/ready`, sign in, and look at a few shipments.
6. **Switch,** either way:
   - Point production at the restore (change the two settings and restart),
     which keeps the old folders untouched.
   - Or stop the server, run
     `python scripts/manage_backups.py activate --restored D:\restore\<id> --backup-id <id> --confirm <id>`,
     and start it. The old folders remain as `<name>.pre-restore-<time>`.
7. **Make a new backup** of the restored state.

If the vector index was not in the backup, rebuild it after step 3 with
`python src/vector_store.py`, with `LOGIDOC_CHROMA_DIR` set to the restored
folder and Ollama running.

## Checking that it works

- **Weekly.** Run `status`: `last_success` should be recent and
  `consecutive_failures` 0.
- **Monthly.** Restore the newest backup into a scratch folder and read the
  report. This is the only real test of a backup.
- **After upgrading the application.** Make a manual backup first
  (`create --label "before upgrade to <commit>"`), and keep it.
