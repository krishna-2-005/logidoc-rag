"""
Release-readiness checks that need no server and no data (run in CI, and before a release is promoted:
docs/RELEASE_RUNBOOK.md, "Versions and promotion").

    python scripts/check_release.py            every check; exit 1 when one fails
    python scripts/check_release.py --version  print the release version only

Checks:
- the version in src/release.py is MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-rc.N;
- CHANGELOG.md starts with an entry for exactly that version, with a date;
- the web application (frontend/package.json) carries the same MAJOR.MINOR.PATCH;
- the published API contract and permission inventory match the code;
- the operational documents a release needs exist;
- local runtime data cannot be committed: the document text cache, the databases, the backups.

Nothing here opens a database, a document or the network.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import release  # noqa: E402

REQUIRED_DOCUMENTS = (
    "CHANGELOG.md", "docs/RELEASE_RUNBOOK.md", "docs/CONFIGURATION_REFERENCE.md", "docs/STAGING_REHEARSAL.md",
    "docs/RELEASE_ACCEPTANCE_CHECKLIST.md", "docs/DOCUMENT_TEXT_CACHE.md", "docs/MIGRATION_REHEARSAL.md",
    "docs/PRODUCTION_DEPLOYMENT.md", "docs/PRODUCTION_BACKUP_AND_RECOVERY.md", "docs/API_PERMISSIONS.md",
    "docs/DEPENDENCY_SECURITY.md",
)

# Paths that must be ignored by git wherever the data folder is the project's own
NEVER_COMMITTED = (
    "data/cache/page_text.cache", "data/audit_reviews.sqlite3", "data/audit_history.sqlite3", "data/auth.sqlite3",
    "data/extractions.sqlite3", "data/integrations.sqlite3", "data/raw/example.pdf", "chroma_db/chroma.sqlite3",
    "backups/logidoc-example/manifest.json", ".env",
)

HEADING = re.compile(r"^## (\S+) \((\d{4}-\d{2}-\d{2})\)\s*$", re.MULTILINE)


def changelog_entry(root=ROOT):
    """(version, date) of the newest entry of CHANGELOG.md, or None."""

    path = root / "CHANGELOG.md"

    if not path.is_file():
        return None

    match = HEADING.search(path.read_text(encoding="utf-8"))

    return match.groups() if match else None


def ignored(paths, root=ROOT):
    """The paths git would ignore (git check-ignore); None when git cannot be asked."""

    try:
        reply = subprocess.run(["git", "check-ignore", *paths], cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None

    if reply.returncode not in (0, 1):
        return None

    return set(reply.stdout.split())


def checks(root=ROOT, contracts=True):
    """[(name, passed, detail)] for every check."""

    found = []

    try:
        release.parts()
        found.append(("version format", True, release.VERSION))
    except ValueError as error:
        found.append(("version format", False, str(error)))
        return found

    entry = changelog_entry(root)
    found.append(("changelog names the version", bool(entry) and entry[0] == release.VERSION,
                  f"newest entry: {entry[0]} ({entry[1]})" if entry else "no entry like '## 1.2.3 (2026-01-31)'"))

    try:
        package = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        package = None

    found.append(("web application version", package == release.base(), f"frontend/package.json: {package}"))

    missing = [name for name in REQUIRED_DOCUMENTS if not (root / name).is_file()]
    found.append(("release documents", not missing, "all present" if not missing else "missing: " + ", ".join(missing)))

    kept_out = ignored(NEVER_COMMITTED, root)

    if kept_out is None:
        found.append(("runtime data is git-ignored", False, "git check-ignore could not be run"))
    else:
        exposed = [name for name in NEVER_COMMITTED if name not in kept_out]
        found.append(("runtime data is git-ignored", not exposed,
                      "cache, databases, documents, index, backups and .env" if not exposed else "not ignored: " + ", ".join(exposed)))

    if contracts:

        import api_permissions
        import export_openapi

        for name, module, produce in (("API contract", export_openapi, export_openapi.contract),
                                      ("permission inventory", api_permissions, api_permissions.document)):
            current = module.TARGET.read_text(encoding="utf-8") if module.TARGET.is_file() else ""
            found.append((f"{name} is published", current == produce(), module.TARGET.name))

    return found


def main(argv=None):

    parser = argparse.ArgumentParser(description="Release-readiness checks.")
    parser.add_argument("--version", action="store_true", help="print the release version and stop")
    arguments = parser.parse_args(argv)

    if arguments.version:
        print(release.VERSION)
        return 0

    results = checks()

    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")

    failed = [name for name, passed, _ in results if not passed]
    print(f"RESULT: {'PASS' if not failed else 'FAIL'} (release {release.VERSION})")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
