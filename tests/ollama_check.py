"""
Ollama availability for tests.

requires_ollama(*models) skips a test, with the reason, when the
Ollama server cannot be reached or a required model is not pulled.
The check runs once per set of models.
"""

import functools
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import ollama

from config import EMBEDDING_MODEL, LLM_MODEL


@functools.lru_cache(maxsize=None)
def ollama_status(*models):
    """
    Returns (available, reason).
    """

    try:
        listing = ollama.list()
    except Exception as error:
        return False, f"Ollama is not reachable ({type(error).__name__})"

    installed = {model.model for model in listing.models}

    missing = [
        model
        for model in models
        if model not in installed and f"{model}:latest" not in installed
    ]

    if missing:
        return False, (
            f"Ollama model(s) not installed: {', '.join(missing)} "
            f"(run: ollama pull <model>)"
        )

    return True, "Ollama available"


def requires_ollama(*models):

    available, reason = ollama_status(*models)

    return pytest.mark.skipif(not available, reason=reason)


requires_embeddings = requires_ollama(EMBEDDING_MODEL)
requires_embeddings_and_llm = requires_ollama(EMBEDDING_MODEL, LLM_MODEL)
