"""
Start the LogiDoc-RAG API (and the React build, when frontend/dist exists).

    python src/api/serve.py

The configuration is checked first: local development mode (no sign-in)
only starts on a loopback address, and the CORS list must name origins.
Environment variables (all optional):

    LOGIDOC_AUTH_MODE      local (default) | required
    LOGIDOC_ENV            production refuses local mode, hides /api/docs and
                           sets Secure cookies
    LOGIDOC_API_HOST       default 127.0.0.1
    LOGIDOC_API_PORT       default 8000
    LOGIDOC_CORS_ORIGINS   comma-separated origins allowed to call the API
                           (default: the Vite dev server on localhost:5173)
    LOGIDOC_COOKIE_SECURE  1 to mark the session cookie Secure (behind HTTPS)
    LOGIDOC_FRONTEND_DIR   the React build to serve (default frontend/dist)
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import uvicorn

from api.app import create_app
from api.core import ApiSettings
from auth import ConfigError


def main():

    settings = ApiSettings.from_env()

    try:
        mode = settings.check_startup()
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    print(f"LogiDoc-RAG API on http://{settings.host}:{settings.port} (sign-in mode: {mode})")

    uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port, log_level="info")

    return 0


if __name__ == "__main__":
    sys.exit(main())
