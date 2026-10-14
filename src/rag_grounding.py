"""
LogiDoc-RAG answer grounding

Finds the retrieved chunks that support an answer, so that only those
are cited, and flags answers that no chunk supports.

This is a lexical check, not semantic entailment. A chunk supports an
answer when a fact the answer states appears in the chunk:

    dates          "October 5, 2026"
    codes          identifiers such as "INV-SHJ-2026-002" or "53-foot",
                   matched whole, ignoring case and separators
    numbers        "$1,550.00", "45" (dates and codes are removed
                   first, so their digits are not counted as numbers)
    names          multi-word capitalised names ("Michael Anderson")

The IDs of the shipments in scope are removed from the answer first:
naming the shipment is not evidence, and every chunk of a shipment
contains its own ID.

An answer is unsupported when it states a date that no chunk contains,
or when it states facts and none of them appears in any chunk. Numbers
may be computed (sums, differences), so a missing number alone does
not make an answer unsupported as long as another fact is found.

Answers without such facts are supported by a chunk only when at least
80% of their content words appear in that chunk.
"""

import re


# ============================================================
# PATTERNS
# ============================================================

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)

DATE_PATTERN = re.compile(
    rf"\b(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b",
    re.IGNORECASE
)

NUMBER_PATTERN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")

# Letters and digits joined by hyphens (any dash), slashes or underscores
CODE_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[\-‐-―/_][A-Za-z0-9]+)+")

NAME_PATTERN = re.compile(r"\b[A-Z][A-Za-z]+(?:[ \-][A-Z][A-Za-z]+)+\b")

WORD_PATTERN = re.compile(r"[a-z]+")

LEADING_ARTICLES = re.compile(r"^(?:The|A|An|This|That)\s+")

ABSTENTION_MARKERS = (
    "could not find",
    "could not generate",
    "could not verify",
    "not enough information",
    "cannot be determined",
    "can't be determined",
    "cannot determine",
    "no information",
    "not mentioned",
    "not provided",
    "not specified",
    "not available",
    "does not contain",
    "do not contain",
    "does not include",
    "do not include",
    "unable to",
)

STOPWORDS = {
    "about", "above", "according", "after", "again", "also", "available",
    "based", "been", "before", "being", "between", "both", "could",
    "document", "documents", "does", "during", "each", "from", "have",
    "information", "into", "logistics", "more", "most", "only", "other",
    "over", "provided", "said", "same", "shows", "some", "such", "than",
    "that", "their", "them", "then", "there", "these", "they", "this",
    "those", "through", "under", "very", "were", "what", "when", "where",
    "which", "while", "will", "with", "would", "your", "listed", "states",
    "stated", "record", "records", "recorded",
}

PROSE_COVERAGE = 0.8


# ============================================================
# FACTS
# ============================================================

def is_abstention(answer):

    text = (answer or "").lower()

    return not text.strip() or any(
        marker in text
        for marker in ABSTENTION_MARKERS
    )


def normalize_date(text):

    return " ".join(text.lower().replace(",", " ").split())


def parse_number(text):

    try:
        return float(text.replace("$", "").replace(",", ""))
    except ValueError:
        return None


def numbers_in(text):
    """
    Numbers in text with dates removed: {value: is_strong}. A number
    is strong when it is an amount ($ or decimals) or has two or
    more digits.
    """

    without_dates = DATE_PATTERN.sub(" ", text)

    numbers = {}

    for token in NUMBER_PATTERN.findall(without_dates):

        value = parse_number(token.rstrip(".,"))

        if value is None:
            continue

        strong = (
            "$" in token
            or "." in token.rstrip(".")
            or sum(character.isdigit() for character in token) >= 2
        )

        numbers[value] = numbers.get(value, False) or strong

    return numbers


def text_key(text):

    return "".join(
        character
        for character in str(text or "").upper()
        if character.isalnum()
    )


def id_pattern(identifier):
    """
    An identifier with any separators (including unicode dashes and
    spaces) between its characters.
    """

    characters = [
        re.escape(character)
        for character in str(identifier)
        if character.isalnum()
    ]

    return re.compile(
        r"(?<![A-Za-z0-9])"
        + r"[\W_]{0,3}".join(characters)
        + r"(?![A-Za-z0-9])",
        re.IGNORECASE
    )


def is_code(token):

    return (
        any(character.isalpha() for character in token)
        and any(character.isdigit() for character in token)
    )


def answer_facts(answer, ignore_ids=()):

    text = " ".join((answer or "").split())

    ignore_keys = {
        text_key(identifier)
        for identifier in ignore_ids
        if text_key(identifier)
    }

    # Whole codes first, so a shipment ID inside a longer code
    # ("INV-SHJ-2026-001") is kept as part of that code
    codes = {
        text_key(token)
        for token in CODE_PATTERN.findall(text)
        if is_code(token)
    } - ignore_keys

    text = CODE_PATTERN.sub(
        lambda match: " " if is_code(match.group(0)) else match.group(0),
        text
    )

    # Then the in-scope shipment IDs written with spaces
    for identifier in ignore_ids:
        if text_key(identifier):
            text = id_pattern(identifier).sub(" ", text)

    names = set()

    for name in NAME_PATTERN.findall(text):

        name = LEADING_ARTICLES.sub("", name)

        if " " in name or "-" in name:
            names.add(name.lower())

    words = [
        word
        for word in WORD_PATTERN.findall(text.lower())
        if len(word) >= 4 and word not in STOPWORDS
    ]

    return {
        "dates": {normalize_date(date) for date in DATE_PATTERN.findall(text)},
        "codes": codes,
        "numbers": numbers_in(text),
        "names": names,
        "words": words,
    }


def chunk_facts(chunk):

    text = " ".join((chunk or "").split())

    return {
        "key": text_key(text),
        "text": text.lower(),
        "dates": {normalize_date(date) for date in DATE_PATTERN.findall(text)},
        "numbers": set(numbers_in(text)),
        "words": set(WORD_PATTERN.findall(text.lower())),
    }


# ============================================================
# GROUNDING
# ============================================================

def ground_answer(answer, chunks, ignore_ids=()):
    """
    Check an answer against chunk texts.

    ignore_ids: shipment IDs in scope, removed from the answer first.

    Returns (supported, supporting chunk indexes, reason).
    """

    facts = answer_facts(answer, ignore_ids)
    parsed = [chunk_facts(chunk) for chunk in chunks]

    strong_numbers = {
        value
        for value, strong in facts["numbers"].items()
        if strong
    }

    weak_numbers = set(facts["numbers"]) - strong_numbers

    # A stated date that no chunk contains is not supported
    missing_dates = {
        date
        for date in facts["dates"]
        if not any(date in chunk["dates"] for chunk in parsed)
    }

    if missing_dates:
        return (
            False,
            [],
            f"Date(s) not found in the documents: {sorted(missing_dates)}",
        )

    def matches(chunk, numbers):

        return (
            bool(facts["dates"] & chunk["dates"])
            or any(code in chunk["key"] for code in facts["codes"])
            or bool(numbers & chunk["numbers"])
            or any(name in chunk["text"] for name in facts["names"])
        )

    has_strong_facts = bool(
        facts["dates"] or facts["codes"] or strong_numbers or facts["names"]
    )

    if has_strong_facts:

        indexes = [
            index
            for index, chunk in enumerate(parsed)
            if matches(chunk, strong_numbers)
        ]

        if indexes:
            return True, indexes, "Stated facts found in the documents"

        return False, [], "None of the stated facts appear in the documents"

    if weak_numbers:

        indexes = [
            index
            for index, chunk in enumerate(parsed)
            if weak_numbers & chunk["numbers"]
        ]

        if indexes:
            return True, indexes, "Stated numbers found in the documents"

        return False, [], "None of the stated numbers appear in the documents"

    words = set(facts["words"])

    if not words:
        return False, [], "The answer states nothing that can be checked"

    indexes = [
        index
        for index, chunk in enumerate(parsed)
        if len(words & chunk["words"]) / len(words) >= PROSE_COVERAGE
    ]

    if indexes:
        return True, indexes, "The answer's wording is found in the documents"

    return False, [], "The answer's wording is not found in the documents"
