"""
LogiDoc-RAG assistant intent classifier (deterministic, offline)

Maps a question to one of the assistant's supported intents, or says why it
cannot. It only chooses WHICH read-only tool family to use: it never grants
access, never picks a shipment, never touches a document and never sees more
than the question text. Authorization, shipment scope and tool argument
validation stay where they were (assistant_tools); an intent cannot override
them.

Method
    1. Normalize: lower-case, keep letters/digits/$, drop punctuation, then a
       conservative stem (plural -s/-es, -ed, -ing, -ly) so "boxes", "billed"
       and "billing" match "box", "bill".
    2. Unsupported-topic guard: a question about something the audit does not
       hold (drivers, addresses, weather, poems...) is "none", even when it
       contains a word such as "delivery".
    3. Score each topical intent: every cue (a stem, or a 2-3 word phrase) that
       occurs adds its weight once. Phrases are checked on the stemmed word
       sequence, not as substrings, so "ok" cannot match inside "book".
    4. Decide:
         best score < MIN_SCORE                -> "none" (insufficient)
         a different intent within TIE_MARGIN  -> "ambiguous" (ask which)
         otherwise                             -> the best intent
       "evidence" is a modifier: chosen only when no topical intent reaches
       MIN_SCORE, because every answer already carries its sources.

The result keeps the scores and the matched cues, so a routing decision can be
explained and tested.
"""

import re
from dataclasses import dataclass, field

MIN_SCORE = 2.0
TIE_MARGIN = 0.75

# A more specific intent wins over the broader topic it refines when both are
# signalled ("variance on the invoice" is about exposure, not the invoice total)
REFINES = {"exposure": "invoice"}

STRONG, MEDIUM, WEAK = 3.0, 2.0, 1.0

# intent -> {cue: weight}. A cue with a space is a phrase over stemmed words.
CUES = {
    "quantity": {
        "piece": STRONG, "quantity": STRONG, "shortage": STRONG, "short": MEDIUM, "unit": MEDIUM, "carton": STRONG,
        "box": STRONG, "pallet": STRONG, "crate": STRONG, "item": MEDIUM, "count": MEDIUM, "damaged": WEAK,
        "how many": MEDIUM, "make it": WEAK, "got there": MEDIUM, "go missing": WEAK, "over deliver": STRONG,
        "overage": STRONG, "overdeliver": STRONG,
    },
    "delivery": {
        "deliver": MEDIUM, "delivery": MEDIUM, "late": STRONG, "delay": STRONG, "on time": STRONG, "early": STRONG,
        "arrive": MEDIUM, "arrival": MEDIUM, "eta": STRONG, "schedule": STRONG, "due date": STRONG, "past due": STRONG,
        "behind schedule": STRONG, "reach": WEAK, "date": MEDIUM, "punctual": STRONG, "transit time": STRONG,
        "on schedule": STRONG, "timing": MEDIUM, "turn up": MEDIUM, "show up": MEDIUM, "when did": MEDIUM, "when was": MEDIUM,
    },
    "invoice": {
        "invoice": STRONG, "bill": STRONG, "billing": STRONG, "charge": STRONG, "fuel": MEDIUM, "surcharge": STRONG,
        "freight cost": STRONG, "cost": MEDIUM, "total": MEDIUM, "amount": MEDIUM, "price": MEDIUM, "line item": STRONG,
        "add up": MEDIUM, "how much": MEDIUM, "pay": WEAK, "paid": MEDIUM, "fee": MEDIUM, "rate": WEAK,
        "cost breakdown": STRONG, "breakdown": WEAK,
    },
    "exposure": {
        "exposure": STRONG, "variance": STRONG, "financial": STRONG, "overbill": STRONG, "overcharge": STRONG,
        "owed": STRONG, "payable": STRONG, "recover": STRONG, "refund": STRONG, "money back": STRONG, "back from": MEDIUM,
        "dispute": MEDIUM, "claim": MEDIUM, "at risk": STRONG,
    },
    "completeness": {
        "missing document": STRONG, "paperwork": STRONG, "document": MEDIUM, "pod": STRONG, "bol": STRONG,
        "complete": MEDIUM, "completeness": STRONG,
        "outstanding": MEDIUM, "received": MEDIUM, "which documents": STRONG, "what documents": STRONG,
        "have we got": MEDIUM, "do we have": MEDIUM, "do we hold": MEDIUM, "on file": MEDIUM, "missing": MEDIUM, "lacking": MEDIUM,
        "still need": MEDIUM, "file": WEAK,
    },
    "summary": {
        "status": STRONG, "summary": STRONG, "summarize": STRONG, "overview": STRONG, "issue": STRONG, "problem": STRONG,
        "discrepancy": STRONG, "clean": STRONG, "audit": WEAK, "wrong": MEDIUM, "everything fine": STRONG,
        "all good": STRONG, "all ok": STRONG, "all okay": STRONG, "anything wrong": STRONG, "how is": WEAK,
        "red flag": STRONG, "go through": WEAK, "state of": MEDIUM, "ok": WEAK, "fine": WEAK, "exception": MEDIUM,
        "any trouble": STRONG, "what happened": MEDIUM, "rundown": STRONG, "run down": STRONG, "everything": WEAK, "looking": WEAK,
    },
}

EVIDENCE_CUES = {
    "evidence": STRONG, "source": STRONG, "proof": MEDIUM, "prove": MEDIUM, "cite": STRONG, "citation": STRONG,
    "which page": STRONG, "what page": STRONG, "where does": MEDIUM, "where in": MEDIUM, "show me where": STRONG,
    "back up": MEDIUM, "back that up": MEDIUM, "reference": MEDIUM,
}

# Things the audit does not hold: the question is "none" however it is worded
UNSUPPORTED = {
    "driver", "address", "phone", "email", "weather", "poem", "joke", "football", "recipe", "colour", "color", "song",
    "temperature", "gps", "map", "salary", "password", "prompt", "stock market", "translate", "who is", "route plan",
    "fuel price", "price of fuel", "contract", "insurance policy", "tool list", "raw text", "raw document", "system prompt",
    # External events and causes: the audit records what the documents say, not why it happened
    "snowstorm", "storm", "traffic", "accident", "truck breakdown", "vehicle breakdown", "strike", "holiday",
    # Facts that live on the documents but that no audit tool returns
    "license plate", "licence plate", "trailer number", "seal number", "container number", "weight", "commodity",
    "who signed", "signature", "name and title",
    # General knowledge about the industry, not about this shipment
    "freight class", "ltl", "ftl", "in general", "incoterm", "hazmat",
}

# The assistant is read-only. A request that starts with one of these verbs
# (optionally after "please", "can you"...) asks it to DO something, whatever
# else the sentence mentions
ACTION_VERBS = {
    "pay", "delete", "remove", "email", "send", "cancel", "approve", "reject", "update", "edit", "change", "create",
    "submit", "resolve", "close", "dismiss", "assign", "print", "reveal", "dump", "export", "forward", "schedule",
    "book", "order", "call", "message", "notify", "escalate", "reopen", "write",
    "dispute", "raise", "lodge", "contact", "chase", "mark", "ping", "reimburse", "void", "reassign",
}

# Consequential actions are also recognised mid-sentence ("... and file a dispute")
ACTION_PHRASES = re.compile(
    r"\b(file|raise|open|lodge|submit|start)\s+(a|an|the|my)?\s*(dispute|claim|ticket|case|complaint)\b"
    r"|\b(escalate|forward|send|email)\s+(it|this|that|them|these)\b"
)

# Words that begin a clause without changing what it asks ("ok, escalate it")
FILLERS = {"ok", "okay", "so", "also", "hey", "hi", "well", "and", "then", "now", "alright", "right", "pls"}
POLITE = re.compile(r"^(please|kindly|can you|could you|would you|will you|i want you to|i need you to|go ahead and|just)\s+")

# Completeness cues that only name documents; without "missing", "complete" or "on file" they
# do not ask whether anything is absent
GENERIC_DOCUMENT_CUES = {"document", "what documents", "which documents", "file"}

# Document names also appear in quantity and delivery questions ("pieces on the BOL")
DOCUMENT_NAMES = {"pod", "bol"}

CONJUNCTION = re.compile(r"\b(and|also|plus|as well as)\b|,")

# A question containing only one of these is too short to classify
GENERIC_ONLY = {"help", "hi", "hello", "thanks", "ok", "yes", "no"}

STEM_EXCEPTIONS = {"status": "status", "bus": "bus", "gas": "gas", "late": "late", "pod": "pod", "bol": "bol",
                   "series": "series", "delivery": "delivery", "deliveries": "delivery", "arrival": "arrival",
                   "billed": "bill", "billing": "billing", "charged": "charge", "charges": "charge",
                   "charging": "charge", "boxes": "box", "pieces": "piece", "paid": "paid", "received": "received",
                   "missing": "missing", "damaged": "damaged", "overcharged": "overcharge", "overbilled": "overbill",
                   "overbilling": "overbill", "delayed": "delay", "delays": "delay", "delivered": "deliver",
                   "delivering": "deliver", "arrived": "arrive", "arrives": "arrive", "arriving": "arrive",
                   "scheduled": "schedule", "documents": "document", "summarise": "summarize", "issues": "issue",
                   "problems": "problem", "items": "item", "units": "unit", "cartons": "carton", "pallets": "pallet",
                   "crates": "crate", "fees": "fee", "prices": "price", "costs": "cost", "totals": "total",
                   "amounts": "amount", "sources": "source", "pages": "page", "claims": "claim", "refunds": "refund",
                   "discrepancies": "discrepancy", "exceptions": "exception", "wrongly": "wrong", "owing": "owed",
                   "recovered": "recover", "recovering": "recover", "reached": "reach", "reaches": "reach",
                   "proves": "prove", "proved": "prove", "showed": "show", "lacking": "lacking", "outstanding": "outstanding",
                   "shortages": "shortage", "shorted": "short", "turned": "turn", "made": "make", "got": "got"}


VOCABULARY = {
    word for cues in (*CUES.values(), EVIDENCE_CUES) for cue in cues for word in cue.split()
}


@dataclass
class Classification:
    intent: str                       # a supported intent, "none" or "ambiguous"
    reason: str                       # why, in words (for tests and the report)
    scores: dict = field(default_factory=dict)
    matched: dict = field(default_factory=dict)
    candidates: tuple = ()            # the tied intents when ambiguous


def stem(word):
    if word in STEM_EXCEPTIONS:
        return STEM_EXCEPTIONS[word]

    if word.endswith(("ss", "is", "us")):
        return word

    for suffix in ("ies", "ing", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)] + ("y" if suffix == "ies" else "")

            # charging -> charg -> charge, when the cue vocabulary has the "e" form
            return base + "e" if base + "e" in VOCABULARY and base not in VOCABULARY else base

    return word


def normalize(question):
    """Stemmed words of the question, in order."""

    text = str(question).lower().replace("’", "'")
    text = re.sub(r"'s\b", " is", text)
    text = re.sub(r"[^a-z0-9$ ]+", " ", text)

    # "bill of lading" and "proof of delivery" are document names, not billing or delivery words
    text = text.replace("bill of lading", "bol").replace("proof of delivery", "pod")

    return [stem(word) for word in text.split()]


def contains_phrase(words, phrase):
    parts = [stem(p) for p in phrase.split()]
    n = len(parts)

    return any(words[i:i + n] == parts for i in range(len(words) - n + 1))


def score(words, cues):
    """(score, matched cues): each cue counts once; a cue inside a longer matched phrase still counts."""

    total, matched = 0.0, []

    for cue, weight in cues.items():
        if contains_phrase(words, cue):
            total += weight
            matched.append(cue)

    return total, matched


def is_action_request(question):
    """
    A request that asks the assistant to DO something consequential (pay, file a
    dispute, escalate, delete, send, update...). Policy:

      - every clause is checked ("looks bad, escalate it to my manager"): a
        clause is an action when it BEGINS with an action verb, after polite
        wording ("please", "can you") and filler ("ok", "also");
      - a clear action phrase ("file a dispute", "escalate it") is an action
        anywhere in the text;
      - a question ABOUT an action is not one: "How would I dispute this
        charge?" and "Can I dispute this?" begin with "how" and "can", not with
        the verb, so they stay informational questions.

    This runs before any tool or model call and does not look at shipments or
    permissions.
    """

    text = str(question).lower()

    if ACTION_PHRASES.search(text) and not re.match(r"\s*(how|what|when|where|why|who|whom|which|can i|could i|should i|do i|may i|is|are|does|did|will|would)\b", text):
        return True

    for clause in re.split(r"[.!?;,]+|\band then\b|\bthen\b", text):
        words = clause.strip().split()

        while words and words[0].strip("'\"") in FILLERS:
            words = words[1:]

        clause = " ".join(words)

        # "could you please ..." has two polite prefixes
        while POLITE.match(clause):
            clause = POLITE.sub("", clause, count=1)
        first = re.sub(r"[^a-z]", "", clause.split()[0]) if clause.split() else ""

        if first in ACTION_VERBS:
            return True

    return False


def classify(question):
    words = normalize(question)

    if is_action_request(question):
        return Classification("none", "action request: the assistant is read-only")

    if not words or (len(words) == 1 and words[0] in GENERIC_ONLY):
        return Classification("none", "too short to classify")

    if any(contains_phrase(words, term) for term in UNSUPPORTED):
        return Classification("none", "unsupported topic")

    scores, matched = {}, {}

    for intent, cues in CUES.items():
        scores[intent], matched[intent] = score(words, cues)

    # Document names ("pieces on the BOL") do not by themselves make a question about completeness
    others = any(value >= MIN_SCORE for name, value in scores.items() if name != "completeness")
    names = sum(CUES["completeness"][c] for c in matched["completeness"] if c in DOCUMENT_NAMES)

    if others and scores["completeness"] - names < MIN_SCORE:
        scores["completeness"] -= names
        matched["completeness"] = [c for c in matched["completeness"] if c not in DOCUMENT_NAMES]

    # "What documents back up your answer?" asks for sources, not for what is missing: when the
    # only completeness signal is a generic "documents" wording and an evidence cue is present,
    # the question is about evidence
    evidence_score, evidence_matched = score(words, EVIDENCE_CUES)

    if evidence_score >= MIN_SCORE and set(matched["completeness"]) <= GENERIC_DOCUMENT_CUES:
        others_without_documents = [v for n, v in scores.items() if n != "completeness"]

        if not any(v >= MIN_SCORE for v in others_without_documents):
            return Classification("evidence", "evidence cue; documents only as the thing cited", scores, {**matched, "evidence": evidence_matched})

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best, best_score = ranked[0]

    if best_score < MIN_SCORE:
        evidence, evidence_matched = score(words, EVIDENCE_CUES)

        if evidence >= MIN_SCORE:
            return Classification("evidence", "evidence cue and no topical cue", scores, {**matched, "evidence": evidence_matched})

        return Classification("none", "no intent reached the minimum score", scores, matched)

    close = [intent for intent, value in ranked[1:] if value >= MIN_SCORE and best_score - value < TIE_MARGIN]

    if close and all(REFINES.get(best) == other for other in close):
        close = []

    # "Was it late and how many pieces were short?": two requests are not one
    if not close and CONJUNCTION.search(str(question).lower()):
        close = [i for i, v in ranked[1:] if v >= MIN_SCORE and REFINES.get(best) != i and REFINES.get(i) != best]

    if close:
        return Classification("ambiguous", f"signals for {best} and {', '.join(close)} are too close", scores, matched, tuple([best] + close))

    return Classification(best, f"{best} score {best_score:g}", scores, matched)
