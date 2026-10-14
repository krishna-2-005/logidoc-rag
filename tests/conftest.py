"""
Shared pytest setup for LogiDoc-RAG.

- Makes the project root, src/ and tests/ importable.
- Guards production data for the whole session: the live data/raw
  folder and chroma_db index must be unchanged afterwards (file names,
  sizes and modification times), and no ingestion staging folder may
  be left behind in the system temp folder.
- Removes temporary test workspaces from earlier runs (ChromaDB can
  keep files open on Windows until the process exits).
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config


TEMP_PREFIXES = (
    "logidoc_ingesttest_",
    "logidoc_uploadtest_",
    "logidoc_intelligencetest_",
    "logidoc_multi_",
    "logidoc_ragtest_",
    "logidoc_eval_",
)


def production_snapshot():

    state = {}

    for folder in (config.RAW_DATA_DIR, config.CHROMA_DIR):

        for path in sorted(Path(folder).rglob("*")):

            if path.is_file():

                state[str(path)] = (
                    path.stat().st_size,
                    path.stat().st_mtime,
                )

    return state


def staging_folders():

    return set(Path(tempfile.gettempdir()).glob("logidoc_staging_*"))


def remove_temp_workspaces():

    for prefix in TEMP_PREFIXES:

        for folder in Path(tempfile.gettempdir()).glob(f"{prefix}*"):
            shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def protect_production_data():

    remove_temp_workspaces()

    production_before = production_snapshot()
    staging_before = staging_folders()

    yield

    leftovers = staging_folders() - staging_before

    for folder in leftovers:
        shutil.rmtree(folder, ignore_errors=True)

    assert production_snapshot() == production_before, (
        "Production data/raw or chroma_db changed during the tests"
    )

    assert not leftovers, (
        f"Ingestion staging folders were left behind: {sorted(leftovers)}"
    )
