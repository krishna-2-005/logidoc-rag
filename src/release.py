"""
The release this code is (docs/RELEASE_RUNBOOK.md, "Versions").

VERSION is the one place the release is named. It follows Semantic Versioning
(MAJOR.MINOR.PATCH, with -rc.N for a release candidate), is the heading of the
newest entry of CHANGELOG.md, and its MAJOR.MINOR.PATCH is the version of the
web application (frontend/package.json): scripts/check_release.py and
tests/test_release.py fail when they disagree.

It is not the API contract version (/api/v1, src/api/app.py API_VERSION), which
only changes with a change that is not backward compatible.

The running server reports it to signed-in users (GET /api/settings,
"release"), in its start-up log line and with `python src/api/serve.py
--version`. It is a public label: no commit, path or setting is part of it.
"""

import re

VERSION = "1.0.0-rc.1"

PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")


def parts(version=VERSION):
    """(major, minor, patch, release candidate number or None); ValueError for anything else."""

    match = PATTERN.match(version)

    if match is None:
        raise ValueError(f"{version!r} is not MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-rc.N")

    major, minor, patch, candidate = match.groups()

    return int(major), int(minor), int(patch), int(candidate) if candidate else None


def base(version=VERSION):
    """MAJOR.MINOR.PATCH, without the release candidate part."""

    major, minor, patch, _ = parts(version)

    return f"{major}.{minor}.{patch}"


def is_candidate(version=VERSION):

    return parts(version)[3] is not None
