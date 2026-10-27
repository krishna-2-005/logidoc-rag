"""
requirements.txt must install everything the application needs.

- Every third-party package that app.py, config.py or src/ imports is pinned.
- Packages that are loaded without an import of our own are pinned too:
  Streamlit's st.pdf ("View Original PDF") needs the separate streamlit-pdf
  component, which a clean install from requirements.txt once lacked, so the
  viewer failed with "The PDF viewer requires the streamlit-pdf component".
- st.pdf works in Streamlit's own test runtime with a fixture PDF.

Run from the project root:
    python -m pytest tests/test_dependency_completeness.py
"""

import ast
import importlib.metadata
import re
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Imported names that are not distributions of their own
LOCAL_SCRIPTS = {"evaluate"}                 # scripts/evaluate.py, put on sys.path by assistant_eval
IMPORT_ALIASES = {"fitz": "pymupdf"}         # PyMuPDF's older import name

# Needed at run time although nothing of ours imports them
LOADED_INDIRECTLY = {"streamlit-pdf": "Streamlit's st.pdf viewer"}


def normalized(name):

    return re.sub(r"[-_.]+", "-", name.lower())


def pinned():

    lines = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    return {normalized(line.split("==")[0]): line.split("==")[1].strip() for line in lines if "==" in line}


def imported_distributions():

    files = [PROJECT_ROOT / "app.py", PROJECT_ROOT / "config.py"] + [
        path for path in (PROJECT_ROOT / "src").rglob("*.py")
        if not path.name.endswith("_backup.py") and "__pycache__" not in path.parts
    ]
    local = {path.stem for path in (PROJECT_ROOT / "src").rglob("*.py")} | {"config", "app", "api"} | LOCAL_SCRIPTS
    providers = importlib.metadata.packages_distributions()
    found = {}

    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):

            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            else:
                continue

            for name in names:
                if name in local or name in sys.stdlib_module_names:
                    continue

                for distribution in providers.get(name) or [IMPORT_ALIASES.get(name, name)]:
                    found.setdefault(normalized(distribution), set()).add(path.relative_to(PROJECT_ROOT).as_posix())

    return found


def test_every_third_party_package_the_application_imports_is_pinned():

    pins = pinned()
    missing = {name: sorted(files) for name, files in imported_distributions().items() if name not in pins}

    assert missing == {}


def test_packages_loaded_without_an_import_of_ours_are_pinned_and_installed():

    pins = pinned()

    for name, reason in LOADED_INDIRECTLY.items():
        assert name in pins, f"{name} ({reason}) is not in requirements.txt"
        assert importlib.metadata.version(name) == pins[name], f"{name}: installed version differs from the pin"


def test_the_streamlit_pdf_viewer_works():

    def script():
        import streamlit as st

        st.pdf("tests/fixtures/shipment_SHJ-2026-001/BOL_SHJ-2026-001.pdf", height=300)
        st.write("after the viewer")

    at = AppTest.from_function(script, default_timeout=60).run()

    assert not at.exception, [item.value for item in at.exception]
    assert any("after the viewer" in str(item.value) for item in at.markdown)
