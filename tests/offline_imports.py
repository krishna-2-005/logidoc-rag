"""
Import helper for tests that need no PDF reader, ChromaDB or Ollama.

reconciliation and intelligence_service import modules that only a full
install provides (pymupdf, chromadb, ollama), although the tests here
replace everything that would call them. stubbed_services() lets those
imports succeed when a package is not installed, then removes the
stubs and every module imported with them from sys.modules, so other
tests still see an honest ImportError instead of a stub. When the
packages are installed it does nothing.
"""

import importlib.util
import sys
import types
from contextlib import contextmanager


OPTIONAL_PACKAGES = ("pymupdf", "chromadb", "ollama")


@contextmanager
def stubbed_services():

    missing = [
        name
        for name in OPTIONAL_PACKAGES
        if importlib.util.find_spec(name) is None
    ]

    before = set(sys.modules)

    for name in missing:
        sys.modules[name] = types.ModuleType(name)

    try:

        yield

    finally:

        if missing:

            for name in set(sys.modules) - before:
                del sys.modules[name]
