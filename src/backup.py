"""
LogiDoc-RAG backups: a consistent copy of every persistent store, a manifest
that says what the copy holds, verification, and restores into a separate
folder. Never deletes or changes a source; never overwrites a backup.

What a backup holds (docs/PRODUCTION_BACKUP_AND_RECOVERY.md)

    databases      the six SQLite stores (reviews, audit history, users,
                   ingestion register, extractions, carrier registry), each
                   copied with SQLite's online backup API: a consistent
                   snapshot even while the application writes. Each copy is
                   integrity-checked, its schema version, row counts and
                   foreign-key state recorded
    documents      the PDFs and the ingestion journal (raw/), the archived
                   originals (archive/) and the retry copies (quarantine/):
                   each copied with its SHA-256 and compared with the source
                   afterwards, so a file that changed during the copy is
                   detected
    vector_index   the ChromaDB folder, only while no ingestion can write it
                   (quiesced): the server's ingestion lock held by the
                   server's own backups, or the server stopped for a backup
                   from the command line. chroma.sqlite3 is copied with the
                   backup API and the segment files byte for byte. Without
                   quiescence the index is left out and the manifest says
                   why: it is derived data, rebuilt from the documents
                   (python src/vector_store.py)
    configuration  the non-secret settings that change results (sign-in
                   mode, review policy, freight tolerances, models, chunking)

How a backup is made

    1. one backup at a time per backup folder (a cross-process lock)
    2. free space is checked first; nothing is deleted to make room
    3. everything is written to <backup id>.partial/ with a manifest whose
       status is in_progress
    4. every file is read back and compared with its recorded checksum
    5. the manifest is marked complete, its own checksum written beside it,
       and the folder renamed to <backup id>/: a backup folder without
       .partial was verified. An interrupted or failed backup stays .partial
       (status in_progress or failed) and is never listed as usable

Restores (restore_backup) go to a new folder laid out like the defaults
(data/ and chroma_db/), so the application can be pointed at it with
LOGIDOC_DATA_DIR and LOGIDOC_CHROMA_DIR. Replacing live data
(activate_restore) needs the backup ID typed as confirmation and the server
stopped, and moves the live folders aside instead of deleting them.
"""

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import stat
import time
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from file_lock import FileLock, LockBusy
from redaction import redact

FORMAT = "logidoc-backup"
FORMAT_VERSION = 1

MANIFEST = "manifest.json"
MANIFEST_CHECKSUM = "manifest.sha256"
PARTIAL = ".partial"
LOCK_NAME = ".backup.lock"
RESTORE_REPORT = "restore-report.json"
RESTORE_MARKER = "RESTORE_IN_PROGRESS"

IN_PROGRESS = "in_progress"
COMPLETE = "complete"
FAILED = "failed"

MANUAL = "manual"
SCHEDULED = "scheduled"
ORIGINS = (MANUAL, SCHEDULED)

ID_PATTERN = re.compile(r"^logidoc-(\d{8}T\d{6}Z)-(manual|scheduled)-([0-9a-f]{6})$")

# Store name -> (file name in the default layout, module with its SCHEMA_VERSION, how an older copy is upgraded)
DATABASES = {
    "audit_reviews": ("audit_reviews.sqlite3", "review_store", "upgraded by the application when it opens the store"),
    "audit_history": ("audit_history.sqlite3", "audit_history", "upgraded by the application when it opens the store"),
    "auth": ("auth.sqlite3", "auth", "upgraded by the application when it opens the store"),
    "ingestion_register": ("ingestion_register.sqlite3", "ingestion_register",
                           "upgraded by the application when it opens the store"),
    "extractions": ("extractions.sqlite3", "extraction_store",
                    "upgraded with python scripts/migrate_extraction_store.py (the API refuses an outdated store)"),
    "carrier_registry": ("carrier_registry.sqlite3", "carrier_registry",
                         "upgraded by the application when it opens the store"),
}

# Document folders: name in the backup -> attribute of Sources
DOCUMENT_FOLDERS = (("raw", "raw_dir"), ("archive", "archive_dir"), ("quarantine", "quarantine_dir"))

# Transient files of an ingestion in progress (also ignored by git): never backed up
TRANSIENT = re.compile(r"^\..*\.(part|new)$")

CHROMA_DATABASE = "chroma.sqlite3"
SQLITE_SIDE_FILES = ("-journal", "-wal", "-shm")

COPY_ATTEMPTS = 3
CHUNK = 1024 * 1024

# Windows' classic path limit: SQLite before 3.45 (Python 3.10 bundles 3.40) cannot open a longer path, so a
# backup or restore that would write one is refused up front with a clear message. None: no limit checked.
MAX_PATH = 259 if os.name == "nt" else None


# ============================================================
# ERRORS
# ============================================================

class BackupError(Exception):
    """A backup or restore that could not be completed. category: a fixed word for metrics and alerts."""

    category = "backup_failed"


class BackupBusy(BackupError):
    category = "busy"


class InsufficientSpace(BackupError):
    category = "insufficient_space"


class SourceChanged(BackupError):
    category = "source_changed"


class VerificationFailed(BackupError):
    category = "verification_failed"


class RestoreRefused(BackupError):
    category = "restore_refused"


class PathTooLong(BackupError):
    category = "path_too_long"


def check_path_lengths(folder, relatives, error=PathTooLong):
    """Refuses before anything is written when folder/relative would exceed MAX_PATH."""

    if MAX_PATH is None:
        return

    base = len(str(Path(folder).resolve()))
    longest = max((len(relative) for relative in relatives), default=0)

    if base + 1 + longest > MAX_PATH:
        raise error(f"Paths in this folder would be {base + 1 + longest} characters long, more than Windows allows "
                    f"({MAX_PATH}). Use a folder with a shorter path (at most {MAX_PATH - longest - 1} characters).")


def category_of(error):

    if isinstance(error, BackupError):
        return error.category
    if isinstance(error, sqlite3.Error):
        return "database_error"
    if isinstance(error, OSError):
        return "storage_error"
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "interrupted"

    return "unexpected_error"


def safe_message(error):
    """An error's message without paths, credentials or long text."""

    return redact(str(error) or type(error).__name__, limit=300)


# ============================================================
# SOURCES
# ============================================================

@dataclass(frozen=True)
class Sources:
    """Where the live data is. Built from audit_service.Locations and the user database."""

    raw_dir: Path
    chroma_dir: Path
    archive_dir: Path
    quarantine_dir: Path
    databases: dict

    @staticmethod
    def from_locations(locations, auth_db):

        return Sources(
            Path(locations.raw_dir), Path(locations.chroma_dir), Path(locations.archive_dir),
            Path(locations.quarantine_dir),
            {
                "audit_reviews": Path(locations.review_db),
                "audit_history": Path(locations.history_db),
                "auth": Path(auth_db),
                "ingestion_register": Path(locations.register_db),
                "extractions": Path(locations.extraction_path()),
                "carrier_registry": Path(locations.carrier_registry_path()),
            },
        )


def supported_versions():
    """{store: the schema version this code writes}: a backup with a newer store cannot be restored for it."""

    import importlib

    return {name: importlib.import_module(module).SCHEMA_VERSION for name, (_, module, _) in DATABASES.items()}


# ============================================================
# SMALL HELPERS
# ============================================================

def utc_now():

    return datetime.now(timezone.utc)


def iso(moment):

    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text):

    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def new_id(moment, origin):

    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}")

    return f"logidoc-{moment.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{origin}-{secrets.token_hex(3)}"


def sha256_file(path):

    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)

    return digest.hexdigest()


RENAME_ATTEMPTS = 20
RENAME_DELAY = 0.1


def retrying(action):
    """
    Runs action (a rename). On Windows a file another process holds open for
    a moment (an antivirus scanner reading a file just written, the search
    indexer) makes a rename fail with PermissionError ("Access is denied"):
    it is tried again for up to two seconds before the error is raised.
    Seen in testing: a backup failed this way on its manifest checksum file.
    """

    for attempt in range(RENAME_ATTEMPTS):
        try:
            return action()
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_DELAY)


def write_atomic(path, text):
    """Writes text to path through a temporary file and a rename (never a half-written file)."""

    path = Path(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")

    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())

    try:
        retrying(lambda: os.replace(temporary, path))
    except OSError:
        try:
            temporary.unlink()  # this function's own temporary file
        except OSError:
            pass
        raise


def is_link(path):
    """A symbolic link or a Windows junction (never followed by a backup, a restore or a cleanup)."""

    try:
        info = os.lstat(path)
    except OSError:
        return False

    if stat.S_ISLNK(info.st_mode):
        return True

    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def files_under(folder):
    """Every regular file under folder (no links, no transient ingestion files), sorted, as (path, posix relative)."""

    folder = Path(folder)

    if not folder.is_dir() or is_link(folder):
        return []

    found = []

    for root, directories, names in os.walk(folder):
        directories[:] = sorted(d for d in directories if not is_link(Path(root) / d))
        for name in sorted(names):
            path = Path(root) / name
            if TRANSIENT.match(name) or is_link(path) or not path.is_file():
                continue
            found.append((path, path.relative_to(folder).as_posix()))

    return found


def read_only(path):
    """A read-only connection; a store in WAL mode without its shared-memory file falls back to a normal one."""

    try:
        connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except sqlite3.OperationalError:
        return sqlite3.connect(str(path), timeout=30)


def quoted(name):

    return '"' + str(name).replace('"', '""') + '"'


# ============================================================
# DATABASE FACTS
# ============================================================

def database_facts(path):
    """
    Integrity, schema version, row count of every table and foreign-key
    problems of a SQLite file, read-only.
    """

    with closing(read_only(path)) as connection:

        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

        try:
            row = connection.execute("SELECT version FROM schema_info").fetchone()
            version = row[0] if row else None
        except sqlite3.Error:
            version = None

        counts = {}
        for table in tables:
            try:
                counts[table] = connection.execute(f"SELECT count(*) FROM {quoted(table)}").fetchone()[0]
            except sqlite3.Error:
                # A virtual table whose module is not loaded here (ChromaDB's full-text search): not counted
                counts[table] = None

        try:
            foreign_key_problems = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        except sqlite3.Error:
            foreign_key_problems = None

    return {
        "integrity": "ok" if integrity == ["ok"] else "; ".join(integrity[:5]),
        "schema_version": version,
        "tables": counts,
        "foreign_key_problems": foreign_key_problems,
    }


def sqlite_copy(source, destination):
    """SQLite's online backup of source into a new file (a consistent snapshot even while it is written)."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        raise BackupError("A backup file already exists; backups are never overwritten.")

    with closing(read_only(source)) as reader, closing(sqlite3.connect(str(destination))) as writer:
        reader.backup(writer)

    return destination.stat().st_size, sha256_file(destination)


def fingerprint(path):
    """Size, modification time and file ID: any write to the file, or its replacement, changes one of them."""

    info = os.stat(path)

    return info.st_size, info.st_mtime_ns, info.st_ino


def file_copy(source, destination):
    """
    Copies source while hashing what was read. The source's fingerprint is
    taken before and after: a file written or replaced during the copy is
    copied again (at most COPY_ATTEMPTS times), then refused. (Measured:
    opening a file is the main cost of a backup on Windows, so the source is
    not opened a second time to hash it again.)
    """

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(COPY_ATTEMPTS):

        before = fingerprint(source)
        digest = hashlib.sha256()
        size = 0

        with open(source, "rb") as reader, open(destination, "wb") as writer:
            for block in iter(lambda: reader.read(CHUNK), b""):
                digest.update(block)
                writer.write(block)
                size += len(block)
            writer.flush()
            os.fsync(writer.fileno())

        if fingerprint(source) == before and size == before[0]:
            shutil.copystat(source, destination)
            return size, digest.hexdigest()

    raise SourceChanged(f"{Path(source).name} kept changing while it was copied; try the backup again when it is idle.")


# ============================================================
# VECTOR INDEX FACTS
# ============================================================

def chromadb_version():

    try:
        import chromadb
        return chromadb.__version__
    except Exception:
        return None


def index_facts(chroma_database):
    """
    What a copy of chroma.sqlite3 holds, read with SQL (no ChromaDB client,
    so the copy is not changed): collections, embeddings, and the documents
    indexed ({source: {sha256: chunks}}). readable False when the layout is
    not the one known (another ChromaDB version): the copy is still kept.
    """

    try:
        with closing(read_only(chroma_database)) as connection:
            collections = [row[0] for row in connection.execute("SELECT name FROM collections ORDER BY name")]
            embeddings = connection.execute("SELECT count(*) FROM embeddings").fetchone()[0]
            indexed = {}
            for source, sha256, chunks in connection.execute(
                "SELECT s.string_value, h.string_value, count(*) FROM embedding_metadata s "
                "LEFT JOIN embedding_metadata h ON h.id = s.id AND h.key = 'sha256' "
                "WHERE s.key = 'source' GROUP BY s.string_value, h.string_value"
            ):
                indexed.setdefault(str(source), {})[str(sha256 or "")] = chunks
    except sqlite3.Error:
        return {"readable": False}

    return {"readable": True, "collections": collections, "embeddings": embeddings, "indexed_documents": indexed}


def consistency(indexed_documents, document_hashes):
    """
    Index against documents: documents with no vectors, and vectors whose
    document (name and content) is not among the documents. Reported, never
    repaired.
    """

    by_name = {name.casefold(): sha for name, sha in document_hashes.items()}
    indexed_names = {source.casefold() for source in indexed_documents}

    not_indexed = sorted(name for name in document_hashes if name.casefold() not in indexed_names)
    orphaned = sorted(
        source for source, hashes in indexed_documents.items()
        if source.casefold() not in by_name or (set(hashes) - {""} and by_name[source.casefold()] not in hashes)
    )

    return {"documents_not_indexed": not_indexed, "vectors_without_document": orphaned}


# ============================================================
# MANIFEST
# ============================================================

def application_version(project_root):
    """{"git_commit": the checked-out commit or None, "python", "chromadb"}, read without running git."""

    commit = None

    try:
        git = Path(project_root) / ".git"
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            loose = git / ref
            if loose.is_file():
                commit = loose.read_text(encoding="utf-8").strip()
            elif (git / "packed-refs").is_file():
                for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        commit = line.split(" ", 1)[0]
        elif re.fullmatch(r"[0-9a-f]{40}", head):
            commit = head
    except OSError:
        commit = None

    return {"git_commit": commit, "python": platform.python_version(), "chromadb": chromadb_version()}


def write_manifest(folder, manifest):

    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    write_atomic(Path(folder) / MANIFEST, text)
    write_atomic(Path(folder) / MANIFEST_CHECKSUM, hashlib.sha256(text.encode("utf-8")).hexdigest() + "\n")


def load_manifest(folder):
    """(manifest or None, problems): the manifest must parse, match its checksum, and be of a known format."""

    folder = Path(folder)
    path = folder / MANIFEST

    if not path.is_file():
        return None, ["The backup has no manifest."]

    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None, ["The manifest cannot be read (corrupted)."]

    problems = []

    try:
        recorded = (folder / MANIFEST_CHECKSUM).read_text(encoding="utf-8").strip()
    except OSError:
        recorded = None

    if recorded != hashlib.sha256(raw).hexdigest():
        problems.append("The manifest does not match its checksum (changed or corrupted).")

    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        return None, problems + ["This is not a LogiDoc-RAG backup manifest."]

    if not isinstance(manifest.get("format_version"), int) or manifest["format_version"] > FORMAT_VERSION:
        problems.append(f"The backup format {manifest.get('format_version')!r} is newer than this application reads "
                        f"({FORMAT_VERSION}).")

    if manifest.get("backup_id") != folder.name.removesuffix(PARTIAL):
        problems.append("The manifest belongs to another backup (its ID differs from the folder name).")

    return manifest, problems


# ============================================================
# MAKING A BACKUP
# ============================================================

def estimate_bytes(sources, include_index=True):

    total = 0

    for path in sources.databases.values():
        if Path(path).is_file():
            total += Path(path).stat().st_size

    for _, attribute in DOCUMENT_FOLDERS:
        total += sum(path.stat().st_size for path, _ in files_under(getattr(sources, attribute)))

    if include_index:
        total += sum(path.stat().st_size for path, _ in files_under(sources.chroma_dir))

    return total


def backup_databases(sources, work, files):

    items = {}

    for name, path in sources.databases.items():

        path = Path(path)

        if not path.is_file():
            items[name] = {"present": False}
            continue

        relative = f"databases/{name}.sqlite3"
        size, sha256 = sqlite_copy(path, work / relative)
        facts = database_facts(work / relative)

        if facts["integrity"] != "ok":
            raise VerificationFailed(f"The copy of the {name} store failed its integrity check: {facts['integrity']}")

        items[name] = {"present": True, "file": relative, "size": size, "sha256": sha256, **facts}
        files.append({"path": relative, "component": "databases", "size": size, "sha256": sha256})

    return {"included": True, "items": items}


def backup_documents(sources, work, files):

    folders = {}
    raw_hashes = {}

    for folder_name, attribute in DOCUMENT_FOLDERS:

        count = 0

        for path, relative in files_under(getattr(sources, attribute)):
            target = f"files/{folder_name}/{relative}"
            size, sha256 = file_copy(path, work / target)
            files.append({"path": target, "component": "documents", "size": size, "sha256": sha256})
            count += 1
            if folder_name == "raw" and "/" not in relative and relative.lower().endswith(".pdf"):
                raw_hashes[relative] = sha256

        folders[folder_name] = count

    return {"included": True, "folders": folders, "pdfs": len(raw_hashes)}, raw_hashes


def backup_index(sources, work, files, raw_hashes):

    chroma = Path(sources.chroma_dir)
    entries = files_under(chroma)

    if not entries:
        return {"included": True, "present": False}

    facts = {}

    for path, relative in entries:

        if relative.endswith(SQLITE_SIDE_FILES):
            continue  # read through the database itself by the backup API

        target = f"vector_index/{relative}"

        if relative == CHROMA_DATABASE:
            size, sha256 = sqlite_copy(path, work / target)
            checked = database_facts(work / target)
            if checked["integrity"] != "ok":
                raise VerificationFailed(f"The copy of the vector index failed its integrity check: {checked['integrity']}")
            facts = index_facts(work / target)
        else:
            size, sha256 = file_copy(path, work / target)

        files.append({"path": target, "component": "vector_index", "size": size, "sha256": sha256})

    result = {"included": True, "present": True, "chromadb_version": chromadb_version(), **facts}

    if facts.get("readable"):
        result["consistency"] = consistency(facts["indexed_documents"], raw_hashes)

    return result


def create_backup(
    sources, backup_root, *, origin=MANUAL, label=None, quiesce=None, include_index=True, index_skip_reason=None,
    configuration=None, application=None, min_free_bytes=0, disk_usage=shutil.disk_usage, now=None,
):
    """
    Makes one verified backup in backup_root and returns (manifest, folder).

    quiesce: a context manager held while documents, databases and the index
    are copied (the server's ingestion lock, or the server's data-folder lock
    taken from the command line). include_index False leaves the vector
    index out (index_skip_reason says why in the manifest). Raises
    BackupBusy, InsufficientSpace or another BackupError; the failed attempt
    stays as <id>.partial with status failed and its error category.
    """

    root = Path(backup_root)
    root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(root / LOCK_NAME)

    try:
        lock.acquire()
    except LockBusy:
        raise BackupBusy("Another backup is running in this backup folder.") from None

    try:

        needed = estimate_bytes(sources, include_index)
        free = disk_usage(root).free

        if free < needed * 1.2 + min_free_bytes:
            raise InsufficientSpace(
                f"Not enough free space for the backup: about {needed / 1048576:.1f} MB needed plus a reserve of "
                f"{min_free_bytes / 1048576:.0f} MB, {free / 1048576:.0f} MB free. Nothing was deleted to make room."
            )

        started = now or utc_now()
        backup_id = new_id(started, origin)
        work = root / (backup_id + PARTIAL)

        planned = [f"databases/{name}.sqlite3-journal" for name in sources.databases]
        planned += [f"files/{folder}/{relative}" for folder, attribute in DOCUMENT_FOLDERS
                    for _, relative in files_under(getattr(sources, attribute))]
        if include_index:
            planned += [f"vector_index/{relative}-journal" for _, relative in files_under(sources.chroma_dir)]
        check_path_lengths(work, planned)

        work.mkdir()

        manifest = {
            "format": FORMAT, "format_version": FORMAT_VERSION, "backup_id": backup_id, "origin": origin,
            "label": (str(label)[:80] if label else None), "status": IN_PROGRESS, "created_at": iso(started),
            "completed_at": None, "application": application or {}, "components": {}, "files": [],
            "totals": {}, "restore_compatibility": {}, "error": None,
        }
        write_manifest(work, manifest)

        try:

            files = []

            with (quiesce() if quiesce else nullcontext()):
                manifest["components"]["databases"] = backup_databases(sources, work, files)
                manifest["components"]["documents"], raw_hashes = backup_documents(sources, work, files)
                manifest["components"]["vector_index"] = (
                    backup_index(sources, work, files, raw_hashes) if include_index else
                    {"included": False, "reason": index_skip_reason or "Not requested."}
                )

            if configuration is not None:
                text = json.dumps(configuration, indent=2, sort_keys=True) + "\n"
                write_atomic(work / "configuration.json", text)
                files.append({"path": "configuration.json", "component": "configuration",
                              "size": len(text.encode("utf-8")), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
                manifest["components"]["configuration"] = {"included": True, "file": "configuration.json"}
            else:
                manifest["components"]["configuration"] = {"included": False}

            manifest["files"] = files
            manifest["totals"] = {"files": len(files), "bytes": sum(item["size"] for item in files)}
            manifest["restore_compatibility"] = {
                "format_version": FORMAT_VERSION,
                "schema_versions": {
                    name: item["schema_version"]
                    for name, item in manifest["components"]["databases"]["items"].items() if item["present"]
                },
                "rule": "Restorable by an application whose stores are at these schema versions or newer; an older "
                        "application cannot open a newer store.",
                "chromadb_version": manifest["components"]["vector_index"].get("chromadb_version"),
            }

            problems = check_files(work, files)

            if problems:
                raise VerificationFailed("The backup did not match what was copied: " + "; ".join(problems[:5]))

            manifest["status"] = COMPLETE
            manifest["completed_at"] = iso(utc_now())
            write_manifest(work, manifest)

            final = root / backup_id
            retrying(lambda: work.rename(final))

            return manifest, final

        except BaseException as error:

            manifest["status"] = FAILED
            manifest["error"] = {"category": category_of(error), "message": safe_message(error)}

            try:
                write_manifest(work, manifest)
            except OSError:
                pass

            raise

    finally:
        lock.release()


# ============================================================
# LISTING AND VERIFYING
# ============================================================

def check_files(folder, files):
    """Problems of the files listed in a manifest: missing, other size or other checksum."""

    problems = []

    for item in files:

        path = Path(folder) / item["path"]

        if not path.is_file():
            problems.append(f"missing {item['path']}")
        elif path.stat().st_size != item["size"]:
            problems.append(f"{item['path']} has another size")
        elif sha256_file(path) != item["sha256"]:
            problems.append(f"{item['path']} does not match its checksum")

    return problems


def list_backups(backup_root):
    """
    The backups in backup_root, newest first: {"backup_id", "origin",
    "created_at", "status", "usable", "label", "bytes", "problems"}. A folder
    still named .partial is incomplete (interrupted or failed), never usable.
    Folders that are not LogiDoc-RAG backups are left out.
    """

    root = Path(backup_root)

    if not root.is_dir():
        return []

    found = []

    for folder in root.iterdir():

        name = folder.name
        partial = name.endswith(PARTIAL)

        if not folder.is_dir() or is_link(folder) or not ID_PATTERN.match(name.removesuffix(PARTIAL)):
            continue

        manifest, problems = load_manifest(folder)
        status = (manifest or {}).get("status", "unknown")

        if partial and status == COMPLETE:
            status = IN_PROGRESS  # completed but never renamed: not trusted

        found.append({
            "backup_id": name.removesuffix(PARTIAL),
            "origin": ID_PATTERN.match(name.removesuffix(PARTIAL)).group(2),
            "created_at": (manifest or {}).get("created_at"),
            "status": status,
            "usable": not partial and not problems and status == COMPLETE,
            "label": (manifest or {}).get("label"),
            "bytes": ((manifest or {}).get("totals") or {}).get("bytes"),
            "error": (manifest or {}).get("error"),
            "problems": problems,
            "incomplete": partial,
        })

    return sorted(found, key=lambda item: item["backup_id"].split("-")[1], reverse=True)


def find_backup(backup_root, backup_id):

    if not ID_PATTERN.match(str(backup_id)):
        raise RestoreRefused("That is not a backup ID (logidoc-<time>-<origin>-<code>).")

    folder = Path(backup_root) / backup_id

    if not folder.is_dir():
        partial = Path(backup_root) / (backup_id + PARTIAL)
        if partial.is_dir():
            raise RestoreRefused(f"{backup_id} is incomplete (interrupted or failed): it cannot be restored.")
        raise RestoreRefused(f"No backup {backup_id} in the backup folder.")

    return folder


def verify_backup(folder):
    """
    {"backup_id", "ok", "problems", "files_checked"}: the manifest is
    intact and complete, every file is present with its size and checksum,
    and every database copy passes SQLite's integrity check.
    """

    folder = Path(folder)
    manifest, problems = load_manifest(folder)

    if manifest is None:
        return {"backup_id": folder.name, "ok": False, "problems": problems, "files_checked": 0}

    if folder.name.endswith(PARTIAL) or manifest.get("status") != COMPLETE:
        problems.append(f"The backup is not complete (status {manifest.get('status')}).")

    files = manifest.get("files") or []
    problems += check_files(folder, files)

    for name, item in ((manifest.get("components") or {}).get("databases") or {}).get("items", {}).items():
        if item.get("present") and (folder / item["file"]).is_file():
            try:
                integrity = database_facts(folder / item["file"])["integrity"]
            except sqlite3.Error:
                integrity = "unreadable"
            if integrity != "ok":
                problems.append(f"the {name} copy fails its integrity check ({integrity})")

    return {"backup_id": manifest.get("backup_id"), "ok": not problems, "problems": problems, "files_checked": len(files)}


def compatibility(manifest, supported=None):
    """{"compatible", "problems", "notes"}: can this application use what the backup holds?"""

    supported = supported or supported_versions()
    problems, notes = [], []

    if manifest.get("format_version", 0) > FORMAT_VERSION:
        problems.append("The backup format is newer than this application reads.")

    for name, version in (manifest.get("restore_compatibility") or {}).get("schema_versions", {}).items():

        current = supported.get(name)

        if current is None:
            notes.append(f"{name}: not a store this application knows; restored as it is.")
        elif version is None:
            notes.append(f"{name}: no schema version recorded (an early store); the application checks it on open.")
        elif version > current:
            problems.append(f"{name} is at schema version {version}, newer than this application ({current}): "
                            f"restore it with the application version that made the backup.")
        elif version < current:
            notes.append(f"{name} is at schema version {version} (this application: {current}); it is "
                         f"{DATABASES.get(name, (None, None, 'upgraded when opened'))[2]}.")

    recorded = (manifest.get("restore_compatibility") or {}).get("chromadb_version")
    installed = chromadb_version()

    if recorded and installed and recorded != installed:
        notes.append(f"The vector index was written by ChromaDB {recorded}; this application has {installed}. "
                     "ChromaDB may upgrade the restored index when it opens it.")

    return {"compatible": not problems, "problems": problems, "notes": notes}


# ============================================================
# RESTORE
# ============================================================

def inside(path, other):

    path, other = Path(path).resolve(), Path(other).resolve()

    return path == other or other in path.parents


def target_of(item):
    """Where a backed-up file goes in the restore folder (the default layout)."""

    path = item["path"]

    if path.startswith("databases/"):
        name = Path(path).stem
        return f"data/{DATABASES.get(name, (name + '.sqlite3',))[0]}"
    if path.startswith("files/"):
        return "data/" + path[len("files/"):]
    if path.startswith("vector_index/"):
        return "chroma_db/" + path[len("vector_index/"):]
    if path == "configuration.json":
        return "restored-configuration.json"

    raise RestoreRefused(f"The manifest lists a file outside the backup layout: {path}")


def safe_relative(path):

    parts = Path(path).parts

    return bool(parts) and not Path(path).is_absolute() and ".." not in parts and ":" not in str(path)


def open_index_facts(chroma_dir, collection_name):
    """The restored index as ChromaDB itself reads it: {"embeddings", "indexed_documents"}."""

    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_dir))

    try:
        collection = client.get_collection(name=collection_name)
    except Exception:
        return {"opened": True, "collection": False, "embeddings": 0, "indexed_documents": {}}

    count = collection.count()
    indexed = {}
    offset = 0

    while offset < count:
        batch = collection.get(include=["metadatas"], limit=1000, offset=offset)
        for metadata in batch["metadatas"] or []:
            metadata = metadata or {}
            if metadata.get("source") is not None:
                hashes = indexed.setdefault(str(metadata["source"]), {})
                key = str(metadata.get("sha256") or "")
                hashes[key] = hashes.get(key, 0) + 1
        offset += 1000

    return {"opened": True, "collection": True, "embeddings": count, "indexed_documents": indexed}


def restore_backup(folder, target, *, protected=(), open_index=True, collection_name="logistics_documents",
                   supported=None, now=None):
    """
    Restores the backup in folder into target (a new or empty folder; never
    a live data, index or backup folder) and returns the restore report,
    also written to target/restore-report.json:

        1-3  the manifest is valid and complete, every file matches its
             checksum, the stores are compatible with this application
        4    files are copied to target/data and target/chroma_db (the
             default layout) and compared with their checksums
        5    every database: integrity, schema version, row counts and
             foreign keys as recorded at backup time
        6    documents and the vector index: the index opened with ChromaDB
             holds the recorded vectors, and its consistency with the
             documents is what it was at backup time

    status "restored" only when every check passed; otherwise "failed" with
    the problems (the restored files are left for inspection).
    Raises RestoreRefused before anything is copied when 1-3 or the target
    are refused.
    """

    folder, target = Path(folder), Path(target)
    started = now or utc_now()

    manifest, problems = load_manifest(folder)

    if manifest is None or problems:
        raise RestoreRefused("The backup manifest is not usable: " + "; ".join(problems))

    if folder.name.endswith(PARTIAL) or manifest.get("status") != COMPLETE:
        raise RestoreRefused(f"The backup is not complete (status {manifest.get('status')}); it cannot be restored.")

    verification = verify_backup(folder)

    if not verification["ok"]:
        raise RestoreRefused("The backup failed verification: " + "; ".join(verification["problems"][:5]))

    compatible = compatibility(manifest, supported)

    if not compatible["compatible"]:
        raise RestoreRefused("The backup is not compatible with this application: " + "; ".join(compatible["problems"]))

    for path in [folder.parent, *protected]:
        if path and (inside(target, path) or inside(path, target)):
            raise RestoreRefused("The restore target must be a separate folder: not a live data, index or backup "
                                 "folder, nor inside or around one.")

    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RestoreRefused("The restore target must be a new or empty folder; nothing is overwritten.")

    check_path_lengths(target, [target_of(item) + "-journal" for item in manifest["files"] if safe_relative(item["path"])]
                       + [RESTORE_REPORT], error=RestoreRefused)

    target.mkdir(parents=True, exist_ok=True)
    (target / RESTORE_MARKER).write_text(f"Restoring {manifest['backup_id']}; not usable until restore-report.json "
                                         "says restored.\n", encoding="utf-8")

    report = {
        "backup_id": manifest["backup_id"], "backup_created_at": manifest.get("created_at"),
        "restored_at": iso(started), "status": FAILED, "steps": {}, "problems": [], "warnings": list(compatible["notes"]),
        "layout": {"data": "data", "vector_index": "chroma_db"},
    }

    report["steps"]["manifest"] = "ok"
    report["steps"]["checksums"] = f"ok ({verification['files_checked']} files)"
    report["steps"]["compatibility"] = "ok"

    # 4. Copy and compare
    for item in manifest["files"]:

        if not safe_relative(item["path"]):
            report["problems"].append(f"unsafe path in the manifest: {item['path']}")
            continue

        destination = target / target_of(item)
        size, sha256 = file_copy(folder / item["path"], destination)

        if sha256 != item["sha256"] or size != item["size"]:
            report["problems"].append(f"{target_of(item)} differs from the backup after copying")

    report["steps"]["copied"] = f"{len(manifest['files'])} files, identical to the backup" if not report["problems"] else "failed"

    # 5. Databases
    databases = {}

    for name, item in manifest["components"]["databases"]["items"].items():

        if not item.get("present"):
            databases[name] = "absent in the backup"
            continue

        restored = target / "data" / DATABASES.get(name, (name + ".sqlite3",))[0]
        found = []

        try:
            facts = database_facts(restored)
        except sqlite3.Error as error:
            databases[name] = {"ok": False}
            report["problems"].append(f"{name}: the restored store cannot be read ({safe_message(error)})")
            continue

        if facts["integrity"] != "ok":
            found.append(f"integrity: {facts['integrity']}")
        if facts["schema_version"] != item["schema_version"]:
            found.append(f"schema version {facts['schema_version']} instead of {item['schema_version']}")
        if facts["tables"] != item["tables"]:
            found.append("row counts differ from the backup")
        if facts["foreign_key_problems"] != item["foreign_key_problems"]:
            found.append(f"{facts['foreign_key_problems']} foreign-key problems instead of {item['foreign_key_problems']}")
        if item["foreign_key_problems"]:
            report["warnings"].append(f"{name}: {item['foreign_key_problems']} foreign-key problems were already in the "
                                      "store when it was backed up.")

        databases[name] = {"ok": not found, "schema_version": facts["schema_version"],
                           "rows": sum(count for count in facts["tables"].values() if count)}
        report["problems"] += [f"{name}: {problem}" for problem in found]

    history = manifest["components"]["databases"]["items"].get("audit_history") or {}
    if history.get("present"):
        tables = history["tables"]
        databases["audit_history"]["preserved"] = {
            key: tables.get(table) for key, table in (("runs", "audit_runs"), ("snapshots", "shipment_snapshots"),
                                                      ("freight_audit_records", "freight_audit_records"))
        }

    report["steps"]["databases"] = databases

    # 6. Documents and the vector index
    documents = manifest["components"]["documents"]
    restored_hashes = {
        Path(item["path"]).name: item["sha256"] for item in manifest["files"]
        if item["path"].startswith("files/raw/") and item["path"].count("/") == 2 and item["path"].lower().endswith(".pdf")
    }
    report["steps"]["documents"] = {"folders": documents.get("folders"), "pdfs": len(restored_hashes)}

    index = manifest["components"]["vector_index"]

    if not index.get("included"):
        report["warnings"].append(f"The vector index is not in this backup ({index.get('reason')}). Rebuild it from "
                                  "the restored documents: python src/vector_store.py with LOGIDOC_CHROMA_DIR set.")
        report["steps"]["vector_index"] = "not in the backup"
    elif not index.get("present"):
        report["steps"]["vector_index"] = "there was no index when the backup was made"
    else:
        step = {"recorded_embeddings": index.get("embeddings")}

        if index.get("readable") and index.get("consistency") is not None:
            now_consistent = consistency(index["indexed_documents"], restored_hashes)
            if now_consistent != index["consistency"]:
                report["problems"].append("the index and the documents disagree differently from the backup")
            for name in now_consistent["documents_not_indexed"]:
                report["warnings"].append(f"{name} has no vectors (it had none when the backup was made either).")
            for name in now_consistent["vectors_without_document"]:
                report["warnings"].append(f"vectors of {name} match no restored document (as at backup time).")

        if open_index:
            try:
                opened = open_index_facts(target / "chroma_db", collection_name)
                step["opened_embeddings"] = opened["embeddings"]
                if index.get("readable") and opened["embeddings"] != index.get("embeddings"):
                    report["problems"].append(f"the restored index holds {opened['embeddings']} vectors, "
                                              f"{index.get('embeddings')} were backed up")
                if index.get("readable") and opened["indexed_documents"] != index.get("indexed_documents"):
                    report["problems"].append("the restored index lists other documents than were backed up")
            except Exception as error:
                report["problems"].append(f"the restored index could not be opened: {safe_message(error)}")

        report["steps"]["vector_index"] = step

    report["status"] = "restored" if not report["problems"] else FAILED
    report["completed_at"] = iso(utc_now())

    write_atomic(target / RESTORE_REPORT, json.dumps(report, indent=2, sort_keys=True) + "\n")

    if report["status"] == "restored":
        (target / RESTORE_MARKER).unlink()

    return report


# ============================================================
# REPLACING LIVE DATA
# ============================================================

def activate_restore(restored, sources, *, backup_id, confirm, server_lock, refuse_inside=None, now=None):
    """
    Puts a validated restore in place of the live data: the live data and
    index folders are renamed to <name>.pre-restore-<time> (kept, never
    deleted), then the restored ones are renamed into their places.

    Refused unless confirm equals backup_id, the restore report says
    restored for that backup, the API server is stopped (its data-folder
    lock is free and held during the swap), every database lives in the
    data folder under its default name, and the restore is on the same drive.
    refuse_inside: a folder the live data must not be (the project folder:
    development data is never swapped).
    """

    restored = Path(restored)

    if confirm != backup_id:
        raise RestoreRefused("Type the backup ID exactly to confirm replacing the live data.")

    try:
        report = json.loads((restored / RESTORE_REPORT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RestoreRefused("The restore folder has no readable restore report.") from None

    if report.get("backup_id") != backup_id or report.get("status") != "restored" or (restored / RESTORE_MARKER).exists():
        raise RestoreRefused("The restore folder was not fully restored and validated from that backup.")

    data_dir = Path(sources.raw_dir).parent
    chroma_dir = Path(sources.chroma_dir)

    if refuse_inside and inside(data_dir, refuse_inside):
        raise RestoreRefused("The live data is the project's own data folder: point LOGIDOC_DATA_DIR and "
                             "LOGIDOC_CHROMA_DIR at the restored folders instead.")

    for name, path in sources.databases.items():
        if Path(path).resolve() != (data_dir / DATABASES[name][0]).resolve():
            raise RestoreRefused(f"The {name} store is configured outside the data folder; replace it by hand.")

    lock = FileLock(server_lock)

    try:
        lock.acquire()
    except LockBusy:
        raise RestoreRefused("The API server is running on this data: stop it first.") from None

    stamp = (now or utc_now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moves, kept = [], []

    try:
        for live, new in ((data_dir, restored / "data"), (chroma_dir, restored / "chroma_db")):

            aside = live.with_name(f"{live.name}.pre-restore-{stamp}")

            if aside.exists():
                raise OSError("a folder kept aside by an earlier restore has the same name")

            if live.exists():
                live.rename(aside)
                moves.append((aside, live))
                kept.append(aside.name)

            if new.exists():
                new.rename(live)
                moves.append((live, new))

    except OSError as error:

        for moved_to, moved_from in reversed(moves):
            try:
                moved_to.rename(moved_from)
            except OSError:
                pass

        raise RestoreRefused(f"The folders could not be swapped ({safe_message(error)}); everything was moved back. "
                             "The restore must be on the same drive as the live data.") from None

    finally:
        lock.release()

    return {"backup_id": backup_id, "kept_aside": kept}


def server_lock_path(data_dir):
    """
    The lock the API server holds while it runs on a data folder: beside the
    folder, not in it, so the folder can be renamed while the lock is held.
    """

    data_dir = Path(data_dir)

    return data_dir.parent / f".{data_dir.name}-server.lock"
