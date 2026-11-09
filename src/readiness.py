"""
LogiDoc-RAG readiness: can this server do its work right now?

/api/health only says that the process answers. /api/ready runs these checks,
each cheap and without side effects except one temporary file:

    documents          the document folder exists and can be listed
    data_writable      new databases can be created where they belong (a
                       temporary file is created and removed in the nearest
                       existing folder; the stores create missing folders)
    extraction_store   its state, read-only (ExtractionStore.inspect)
    model_service      Ollama answers its model list within a short time and
                       holds both configured models (nothing is loaded,
                       embedded or generated)
    databases          every SQLite store the application cannot work without
                       (reviews, audit history, ingestion register, users,
                       carrier registry) that exists opens read-only and its
                       schema can be read (a store not created yet is fine:
                       it is created on first use); unusable stores are named
                       by their role (audit_history, auth, ...). The
                       extraction store keeps its own check
    vector_index       the ChromaDB index database opens read-only (absent
                       is fine: it is created by the first upload)
    disk_space         free space on the data, index and backup volumes:
                       level ok, warning or critical (thresholds of the alert
                       rules, LOGIDOC_ALERT_DISK_*_MB)

Overall status:

    ready       every check passed
    degraded    the folders are usable, but the model service, the
                extraction store, the vector index or the free space is
                not: the audit, documents, history and reports work;
                uploads, generated answers or extraction writes may not.
                An optional AI service being offline is never "not ready"
    not_ready   a folder check failed, or a database cannot be opened

Only status words leave this module: never a path, a host, a model name,
an exception message or document text.
"""

import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import config
import extraction_store as store_module
import model_client

# Replaced in tests
disk_usage = shutil.disk_usage

WARNING_MB = 2048
CRITICAL_MB = 512


READY = "ready"
DEGRADED = "degraded"
NOT_READY = "not_ready"

# Extraction store states the API can read and write as they are
STORE_USABLE = (store_module.ABSENT, store_module.CURRENT)


def listable(folder):

    try:
        folder = Path(folder)
        next(folder.iterdir(), None)
        return True
    except OSError:
        return False


def nearest_existing(path):

    path = Path(path)

    while not path.exists() and path != path.parent:
        path = path.parent

    return path


def writable(folder):

    try:
        with tempfile.TemporaryFile(dir=nearest_existing(folder)):
            return True
    except OSError:
        return False


def check_documents(locations):

    return {"ok": listable(locations.raw_dir)}


def check_data_writable(locations):

    folders = {
        Path(path).parent
        for path in (locations.review_db, locations.history_db, locations.register_db, locations.extraction_path())
    }

    return {"ok": all(writable(folder) for folder in folders)}


def check_extraction_store(locations):

    state = store_module.ExtractionStore.inspect(locations.extraction_path()).state

    return {"ok": state in STORE_USABLE, "state": state}


def check_model_service(list_models=None):

    installed = (list_models or model_client.installed_models)()

    if installed is None:
        return {"ok": False, "reachable": False, "missing": []}

    wanted = {model_client.EMBEDDING: config.EMBEDDING_MODEL, model_client.LANGUAGE_MODEL: config.LLM_MODEL}
    missing = sorted(kind for kind, name in wanted.items() if not model_client.has_model(installed, name))

    return {"ok": not missing, "reachable": True, "missing": missing}


def opens(path):
    """Whether a SQLite file opens read-only and its schema can be read."""

    try:
        with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=2)) as connection:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True
    except sqlite3.Error:
        return False


def stores_of(locations, extra=None):
    """
    {role: database path} of the stores the application cannot work without.
    The extraction store is left out: it has its own check (extraction_store),
    and the audit works without it (degraded, never not ready).
    """

    stores = {
        "audit_reviews": locations.review_db, "audit_history": locations.history_db,
        "ingestion_register": locations.register_db,
    }

    if hasattr(locations, "carrier_registry_path"):
        stores["carrier_registry"] = locations.carrier_registry_path()

    stores.update(extra or {})

    return stores


def check_databases(stores):

    unusable = sorted(role for role, path in stores.items() if Path(path).is_file() and not opens(path))

    return {"ok": not unusable, "unusable": unusable}


def check_vector_index(chroma_dir):

    database = Path(chroma_dir) / "chroma.sqlite3"

    if not database.is_file():
        return {"ok": True, "state": "absent"}

    return {"ok": True, "state": "present"} if opens(database) else {"ok": False, "state": "unreadable"}


def free_mb(folders):
    """{folder role: free MB} of the volume holding each folder (its nearest existing parent)."""

    return {role: disk_usage(nearest_existing(folder)).free / 1048576 for role, folder in folders.items() if folder}


def check_disk_space(folders, warning_mb=WARNING_MB, critical_mb=CRITICAL_MB):

    lowest = min(free_mb(folders).values(), default=None)

    if lowest is None:
        return {"ok": True, "level": "ok"}

    level = "critical" if lowest < critical_mb else "warning" if lowest < warning_mb else "ok"

    return {"ok": level != "critical", "level": level}


def safely(check, *args):
    """
    A check that fails unexpectedly reports "not ok" instead of failing the
    readiness answer (the cause is never returned).
    """

    try:
        return check(*args)
    except Exception:
        return {"ok": False}


def readiness(locations, list_models=None, extra_stores=None, disk_folders=None, warning_mb=WARNING_MB,
              critical_mb=CRITICAL_MB):
    """
    {"status": ready | degraded | not_ready, "checks": {...}}.

    extra_stores: more {role: database path} (the API adds the user store);
    disk_folders: {role: folder} whose volumes are checked (default: the
    data and index folders).
    """

    folders = disk_folders or {"data": Path(locations.raw_dir).parent, "index": locations.chroma_dir}

    checks = {
        "documents": safely(check_documents, locations),
        "data_writable": safely(check_data_writable, locations),
        "extraction_store": safely(check_extraction_store, locations),
        "model_service": safely(check_model_service, list_models),
        "databases": safely(check_databases, stores_of(locations, extra_stores)),
        "vector_index": safely(check_vector_index, locations.chroma_dir),
        "disk_space": safely(check_disk_space, folders, warning_mb, critical_mb),
    }

    if not (checks["documents"]["ok"] and checks["data_writable"]["ok"] and checks["databases"]["ok"]):
        status = NOT_READY
    elif all(check["ok"] for check in checks.values()):
        status = READY
    else:
        status = DEGRADED

    return {"status": status, "checks": checks}
