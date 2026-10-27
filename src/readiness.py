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

Overall status:

    ready       every check passed
    degraded    the folders are usable, but the model service or the
                extraction store is not: the audit, documents, history and
                reports work; uploads, generated answers or extraction
                writes may not
    not_ready   a folder check failed

Only status words leave this module: never a path, a host, a model name,
an exception message or document text.
"""

import tempfile
from pathlib import Path

import config
import extraction_store as store_module
import model_client


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


def safely(check, *args):
    """
    A check that fails unexpectedly reports "not ok" instead of failing the
    readiness answer (the cause is never returned).
    """

    try:
        return check(*args)
    except Exception:
        return {"ok": False}


def readiness(locations, list_models=None):
    """
    {"status": ready | degraded | not_ready, "checks": {...}}.
    """

    checks = {
        "documents": safely(check_documents, locations),
        "data_writable": safely(check_data_writable, locations),
        "extraction_store": safely(check_extraction_store, locations),
        "model_service": safely(check_model_service, list_models),
    }

    if not (checks["documents"]["ok"] and checks["data_writable"]["ok"]):
        status = NOT_READY
    elif all(check["ok"] for check in checks.values()):
        status = READY
    else:
        status = DEGRADED

    return {"status": status, "checks": checks}
