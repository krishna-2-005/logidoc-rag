"""
Test isolation helpers for LogiDoc-RAG.

Tests must not depend on, or write to, the live data/raw folder or
chroma_db index: their contents change whenever documents are
uploaded through the app.

sample_workspace() creates a temporary raw folder holding the
SHJ-2026-001 sample PDFs from tests/fixtures/shipment_SHJ-2026-001
(byte-for-byte copies of the samples originally committed in
data/raw) plus an empty temporary index location, and points every
loaded module at them for the duration of the block.
"""

import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config


SAMPLE_SHIPMENT_DIR = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "shipment_SHJ-2026-001"
)


@contextmanager
def redirect_project_paths(raw_dir, chroma_dir):
    """
    Point every loaded module that bound config.RAW_DATA_DIR or
    config.CHROMA_DIR at import time ("from config import ...") at
    the given folders, including config itself, and restore the
    production paths afterwards.
    """

    replacements = {
        "RAW_DATA_DIR": (config.RAW_DATA_DIR, Path(raw_dir)),
        "CHROMA_DIR": (config.CHROMA_DIR, Path(chroma_dir)),
    }

    patched = []

    for module in list(sys.modules.values()):

        for attribute, (old, new) in replacements.items():

            try:
                value = getattr(module, attribute, None)
            except Exception:
                continue

            if isinstance(value, Path) and value == old:

                setattr(module, attribute, new)
                patched.append((module, attribute, old))

    try:

        yield

    finally:

        for module, attribute, old in patched:
            setattr(module, attribute, old)


@contextmanager
def sample_workspace(prefix, copy_samples=True):
    """
    Temporary raw folder with the SHJ-2026-001 samples and an empty
    index location, with all project paths redirected to them.

    copy_samples=False leaves the raw folder empty, for tests that
    add the samples through the ingestion manager themselves (it
    refuses to index a file that is already in the raw folder).

    Yields (raw_dir, chroma_dir).
    """

    # ChromaDB can keep files open on Windows until the process
    # exits, so folders from earlier runs are removed here
    for stale in Path(tempfile.gettempdir()).glob(f"{prefix}*"):
        shutil.rmtree(stale, ignore_errors=True)

    workspace = Path(tempfile.mkdtemp(prefix=prefix))

    raw_dir = workspace / "raw"
    chroma_dir = workspace / "chroma_db"

    raw_dir.mkdir()

    if copy_samples:

        for pdf_file in sorted(SAMPLE_SHIPMENT_DIR.glob("*.pdf")):
            shutil.copy2(pdf_file, raw_dir / pdf_file.name)

    try:

        with redirect_project_paths(raw_dir, chroma_dir):
            yield raw_dir, chroma_dir

    finally:

        try:

            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()

        except Exception:
            pass

        shutil.rmtree(workspace, ignore_errors=True)
