"""
Start the LogiDoc-RAG API (and the React build, when frontend/dist exists).

    python src/api/serve.py
    python src/api/serve.py --check     (validate the settings, start nothing)

Before anything else the environment is validated (src/environment.py):
any error stops the server with the list of settings to fix; production
needs every setting explicitly (docs/PRODUCTION_DEPLOYMENT.md). Then:

    logs         one handler for every logger: JSON lines in production,
                 text otherwise (LOGIDOC_LOG_FORMAT, LOGIDOC_LOG_LEVEL,
                 LOGIDOC_LOG_FILE for a rotating file as well)
    data lock    the server holds a lock beside the data folder while it
                 runs, so a second server, or a backup or restore from the
                 command line, knows it is running (one writer at a time)
    jobs         the backup schedule and the monitor (alerts) run in this
                 process; LOGIDOC_BACKGROUND_JOBS=0 turns them off
    proxy        uvicorn's own proxy handling and access log are off: the
                 application trusts X-Forwarded-* only from
                 LOGIDOC_FORWARDED_ALLOW_IPS and logs each request itself

Main settings (all optional in development; .env.example lists them all):

    LOGIDOC_ENV            development (default) | test | production
    LOGIDOC_AUTH_MODE      local (default, loopback only) | required
    LOGIDOC_API_HOST       default 127.0.0.1 (behind the reverse proxy)
    LOGIDOC_API_PORT       default 8000
    LOGIDOC_TRUSTED_HOSTS  host names answered (production: required)
    LOGIDOC_CORS_ORIGINS   origins allowed to call the API
    LOGIDOC_DATA_DIR, LOGIDOC_CHROMA_DIR, LOGIDOC_BACKUP_DIR

GET /api/health says the server answers; GET /api/ready says whether the
documents, databases, vector index, extraction store, Ollama models and
free space are usable (ready, degraded or not_ready).

Run exactly one server process per data folder: the ingestion lock that
keeps uploads, backups and document changes apart works inside one process.
"""

import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import environment  # noqa: E402  (imports nothing of the application)


def main(argv=None):

    argv = sys.argv[1:] if argv is None else argv
    report = environment.validate(os.environ)

    if report["errors"]:
        print("Configuration error: the server cannot start.", file=sys.stderr)
        for problem in report["errors"]:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    if "--check" in argv:
        # Validate only: nothing is opened, locked or started
        print(f"Configuration valid for {report['environment']}.")
        for warning in report["warnings"]:
            print(f"  warning: {warning}")
        return 0

    import uvicorn

    import observability
    from api.app import create_app
    from api.core import ApiSettings
    from auth import ConfigError
    from backup import server_lock_path
    from file_lock import FileLock, LockBusy

    production = report["environment"] == environment.PRODUCTION
    observability.configure_logging(
        log_format=(os.environ.get("LOGIDOC_LOG_FORMAT") or ("json" if production else "text")).strip().lower(),
        level=(os.environ.get("LOGIDOC_LOG_LEVEL") or "INFO").strip().upper(),
        log_file=(os.environ.get("LOGIDOC_LOG_FILE") or "").strip() or None,
    )
    log = logging.getLogger("logidoc.server")

    for warning in report["warnings"]:
        log.warning("Configuration: %s", warning)

    settings = ApiSettings.from_env()

    try:
        mode = settings.check_startup()
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    import config

    lock = FileLock(server_lock_path(config.DATA_DIR))

    try:
        lock.acquire()
    except LockBusy:
        print("Another LogiDoc-RAG server, or a backup or restore from the command line, is using this data folder.",
              file=sys.stderr)
        return 3

    settings.background_jobs = (report["environment"] != environment.TEST
                                and (os.environ.get("LOGIDOC_BACKGROUND_JOBS") or "1").strip().lower()
                                not in environment.FALSE)

    log.info("LogiDoc-RAG API on %s:%s (environment %s, sign-in mode %s)", settings.host, settings.port,
             report["environment"], mode)

    # The ingestion lock works inside one process only
    log.info("One writer at a time: do not upload or change documents from the Streamlit app (app.py) while this "
             "server runs on the same data folder.")

    try:
        uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port, log_config=None,
                    access_log=False, proxy_headers=False, server_header=False)
    finally:
        lock.release()

    return 0


if __name__ == "__main__":
    sys.exit(main())
