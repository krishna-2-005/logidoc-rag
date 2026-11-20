"""
Release identification (src/release.py, scripts/check_release.py): one
version, named by the changelog and the web application, reported by the
running server without anything secret, and checked before a release.
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import check_release  # noqa: E402
import release  # noqa: E402
from test_api import Rig  # noqa: E402


def test_the_version_is_a_semantic_version_or_a_release_candidate():

    assert release.parts("1.0.0") == (1, 0, 0, None) and release.parts("2.13.4-rc.7") == (2, 13, 4, 7)
    assert release.base("2.13.4-rc.7") == "2.13.4" and release.is_candidate("2.13.4-rc.7") and not release.is_candidate("1.0.0")

    for wrong in ("1.0", "v1.0.0", "1.0.0-beta", "1.0.0-rc", "1.0.0-rc.1+build", "", "1.0.0 "):
        with pytest.raises(ValueError):
            release.parts(wrong)

    release.parts()  # the version of this code itself


def test_this_release_passes_every_readiness_check():
    """The changelog, the web application, the published contracts, the documents and the ignore rules agree."""

    failed = [(name, detail) for name, passed, detail in check_release.checks() if not passed]

    assert not failed, failed


def test_the_checks_catch_a_changelog_or_a_web_version_left_behind(tmp_path):

    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"version": "0.9.0"}), encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## 0.9.0 (2026-01-01)\n\nOlder.\n", encoding="utf-8")

    results = {name: passed for name, passed, _ in check_release.checks(tmp_path, contracts=False)}

    assert results["version format"] is True
    assert results["changelog names the version"] is False
    assert results["web application version"] is False
    assert results["release documents"] is False

    (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n## {release.VERSION} (2026-11-20)\n\nNew.\n", encoding="utf-8")
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"version": release.base()}), encoding="utf-8")
    results = {name: passed for name, passed, _ in check_release.checks(tmp_path, contracts=False)}

    assert results["changelog names the version"] is True and results["web application version"] is True


def test_the_server_reports_its_release_to_signed_in_users_only(tmp_path):

    rig = Rig(tmp_path, "required")

    settings = rig.client("bob").get("/api/settings")
    assert settings.status_code == 200 and settings.json()["release"] == release.VERSION

    # Not to a visitor, and never in the liveness answer
    assert rig.client().get("/api/settings").status_code == 401
    assert rig.client().get("/api/health").json() == {"status": "ok"}
    assert release.VERSION not in rig.client().get("/api/ready").text


def test_the_server_command_prints_its_version_without_reading_any_setting(monkeypatch, capsys):

    from api import serve

    monkeypatch.setenv("LOGIDOC_ENV", "not-a-profile")        # would stop a start

    assert serve.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"LogiDoc-RAG {release.VERSION}"
    assert check_release.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == release.VERSION


def test_the_staging_rehearsal_refuses_the_installation_data_and_existing_folders(tmp_path):
    """It only ever works in a new folder outside the project (scripts/staging_rehearsal.py)."""

    import staging_rehearsal

    for unsafe in (PROJECT_ROOT / "data", PROJECT_ROOT / "data" / "rehearsal", PROJECT_ROOT / "chroma_db" / "x", PROJECT_ROOT, tmp_path):
        with pytest.raises(staging_rehearsal.Refused):
            staging_rehearsal.check_work_folder(unsafe)

    assert staging_rehearsal.check_work_folder(tmp_path / "new") == (tmp_path / "new").resolve()
    assert staging_rehearsal.main(["--work", str(tmp_path)]) == 2

    # Its settings are the rehearsal's own: nothing of the calling environment's LogiDoc settings is kept
    env = staging_rehearsal.environment(tmp_path / "new", 1, 2, "k" * 40)
    assert env["LOGIDOC_ENV"] == "production" and env["LOGIDOC_DATA_DIR"] == str(tmp_path / "new" / "data")
    assert not [name for name in env if name.startswith("LOGIDOC_") and name.endswith("_DB")]
