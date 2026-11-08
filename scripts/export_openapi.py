"""
Writes the API contract (the OpenAPI document of /api, served as /api/v1 too)
to docs/api/openapi-v1.json, for integrators who cannot reach a running
server. tests/test_api_contracts.py fails when the published file and the code
disagree, so a change to the API is published with it.

The application is built on a temporary folder: no data, database or
document of the installation is opened.

Run from the project root:
    python scripts/export_openapi.py            # write the file
    python scripts/export_openapi.py --check    # exit 1 when it is out of date
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

TARGET = ROOT / "docs" / "api" / "openapi-v1.json"


def contract():
    """The OpenAPI document as a stable JSON text."""

    from api.app import create_app
    from api.core import ApiSettings, ApiState
    from audit_service import Locations
    from review_store import Policy

    folder = Path(tempfile.mkdtemp(prefix="logidoc_openapi_"))
    locations = Locations(folder / "raw", folder / "chroma", folder / "reviews.sqlite3", folder / "history.sqlite3",
                          folder / "register.sqlite3", folder / "quarantine", folder / "archive")
    settings = ApiSettings(environ={"LOGIDOC_AUTH_MODE": "local"})
    app = create_app(ApiState(settings, locations, folder / "auth.sqlite3", Policy()))

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main():

    parser = argparse.ArgumentParser(description="Write or check the published API contract.")
    parser.add_argument("--check", action="store_true", help="exit 1 when docs/api/openapi-v1.json is out of date")
    arguments = parser.parse_args()

    text = contract()

    if arguments.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current != text:
            print(f"{TARGET} is out of date: run python scripts/export_openapi.py")
            return 1
        print(f"{TARGET} is up to date")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET} ({len(text):,} characters)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
