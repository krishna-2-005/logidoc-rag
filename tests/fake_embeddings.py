"""
Deterministic offline embeddings for tests and the offline evaluation.

lexical_embedding() is a hashed bag of words: texts that share words
get similar vectors, so vector search still ranks by wording without
Ollama. It is not a semantic model; offline retrieval scores only show
that retrieval and scoping work, not how well a real embedding model
would rank.
"""

import hashlib
import math
import re

DIMENSIONS = 256


def lexical_embedding(text):

    vector = [0.0] * DIMENSIONS

    for token in re.findall(r"[a-z0-9]+", (text or "").lower()):

        index = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)

        vector[index % DIMENSIONS] += 1.0

    norm = math.sqrt(sum(value * value for value in vector)) or 1.0

    return [value / norm for value in vector]
