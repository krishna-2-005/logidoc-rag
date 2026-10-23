"""Helpers for tests that need an extraction store at an older schema, and the explicit upgrade of it."""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import extraction_migration as migration  # noqa: E402
import extraction_store as store_module  # noqa: E402


def upgrade(path, backups=None):
    """The explicit migration (backup, verification, one transaction), into a backups folder next to the store."""

    backups = Path(backups) if backups else Path(path).parent / "backups"
    backups.mkdir(exist_ok=True)

    return migration.migrate(path, backups)


def store_at(path, version):
    """
    An empty extraction store as schema `version` left it (migrations 1..version applied, nothing newer), built with plain
    sqlite3 so that no application code decides what it looks like.
    """

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")

    for step in range(1, version + 1):
        for statement in store_module.statements(store_module.MIGRATIONS[step]):
            connection.execute(statement)

    connection.execute("INSERT INTO schema_info VALUES (?)", (version,))
    connection.commit()
    connection.close()

    return path
