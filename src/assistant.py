"""
LogiDoc-RAG AI Logistics Assistant

Answers a question about ONE authorized shipment from the audit, using only
the read-only tools of assistant_tools. The answer is composed in Python from
the tool results, so every number, status and citation is deterministic and
comes from the existing audit modules. No language model sees the question
unless a general-question fallback is supplied, and its text is then labelled
as generated and never mixed into the findings.

Response (a dict):

    status          answered | insufficient_evidence | clarification | refused
    shipment_id     the shipment the answer is about (None if not chosen)
    answer          the answer in words (facts only)
    findings        [{label, value, status}] verified facts used
    citations       [{source, page, document_type, shipment_id, text}]
    uncertainty     what could not be verified, in words (empty if nothing)
    next_steps      suggestions (nothing is executed; they need a person)
    tools_used      [{name, arguments}]
    generated       text written by a model (False for composed answers)

Rules: a shipment is needed (chosen in the request, or named in the question);
a question naming a different shipment than the chosen one is refused, not
answered; a value the audit does not have is "not available", never zero; a
shipment is never called clean while anything about it is unverified.
"""

import re

import assistant_tools as tools
import authz
from intent_classifier import classify, is_action_request
from display_text import money_text
from shipment_identity import identifier_key

NA = "not available"

SINGLE_CHARGE = re.compile(r"\b(fuel|surcharge|detention|base freight|accessorial|other charges?|line items?|break ?down)\b")
ONE_CHARGE_UNAVAILABLE = ("Individual charges are not available to the assistant: only the invoice total and the sum of "
                          "its line items are, so this does not state what that charge was.")

READ_ONLY = ("The assistant is read-only: it cannot change, send, approve, pay, dispute, escalate or delete anything. "
             "Those steps need a person, using the Exception Center or Documents screens.")

LABELS = {"quantity": "piece counts", "delivery": "delivery timing", "invoice": "the invoice", "exposure": "financial exposure",
          "completeness": "missing documents", "summary": "the overall status"}


def intent_of(question):
    """The routing decision as a label: an intent, "none" or "ambiguous"."""

    return classify(question).intent


def mentioned_shipments(question, service):
    """Shipments of the snapshot whose identifier appears in the question."""

    squashed = re.sub(r"[^a-z0-9]", "", question.lower())

    return [
        s for s in service.snapshot()["shipments"]
        if s["shipment_key"].lower() in squashed
    ]


def response(status, **fields):
    base = {
        "status": status, "shipment_id": None, "answer": "", "findings": [], "citations": [], "uncertainty": [],
        "next_steps": [], "tools_used": [], "generated": False,
    }
    base.update(fields)

    return base


UNAVAILABLE_NOTE = ("The stored reviewer corrections could not be read, so any approved correction is not reflected in this result. "
                    "Ask an administrator to check the extraction store.")


class Assistant:

    def __init__(self, service, principal, fallback=None):
        """
        fallback: optional callable (question, shipment_id) -> text, used only
        for a question none of the audit tools can answer. Its text is marked
        generated and carries no citations of its own.
        """

        self.service, self.principal, self.fallback = service, principal, fallback
        self.used, self.citations, self.scope = [], [], tools.Scope()
        self.question = ""
        self.corrections = []
        self.corrections_unavailable = False

    def call(self, name, **arguments):
        self.used.append({"name": name, "arguments": arguments})

        data = tools.run_tool(self.principal, self.service, self.scope, name, arguments)

        if name == "get_reconciliation_result":
            self.corrections = data.get("corrections_applied") or []
            self.corrections_unavailable = bool(data.get("corrections_unavailable"))

        return data

    def corrected_note(self):
        """
        The note for an answer that rests on reviewer-approved corrections, or
        None. It names the fields: the cited document does not state those values.
        """

        fields = {c["field"] for c in self.corrections} | {
            "a value" for row in self.citations if row.get("value_origin") == "approved_correction"
        }

        if not fields:
            return None

        names = ", ".join(sorted(f.replace("_", " ") for f in fields if f != "a value")) or "a value"

        return (f"This result uses reviewer-approved correction(s) instead of the document's own text for: {names}. "
                "The cited document does not state the corrected value and no page is given for it.")

    def cite(self, audit, shipment_id):
        data = self.call("get_shipment_evidence", shipment_id=shipment_id, audit=audit)

        for row in data["evidence"]:
            if row not in self.citations:
                self.citations.append(row)

    # ----------------------------------------------------------

    def ask(self, question, shipment_id=None):
        # Authorization comes first, for every kind of question including refusals
        authz.require(self.principal, authz.EXCEPTIONS_VIEW)

        question = (question or "").strip()
        self.question = question

        if not question or len(question) > 1000:
            return response("clarification", answer="Ask a question of at most 1000 characters.")

        if not self.service.has_documents():
            return response("insufficient_evidence", answer="There are no documents to answer from.",
                            next_steps=["Add documents in Document Intelligence."])

        chosen = self.resolve(question, shipment_id)

        if "generated" in chosen:
            return chosen

        self.scope = tools.Scope(chosen["shipment_key"])
        sid = chosen["shipment_id"]
        # Read-only policy: a request to DO something is refused here, after the shipment was
        # authorized and before any classification, tool or language-model call
        if is_action_request(question):
            return response("insufficient_evidence", shipment_id=sid, answer=READ_ONLY)

        decision = classify(question)
        intent = decision.intent

        if intent == "ambiguous":
            names = " or ".join(LABELS.get(c, c) for c in decision.candidates)
            return response("clarification", shipment_id=sid,
                            answer=f"Your question could mean more than one thing ({names}). Ask about one at a time.")

        if intent == "none":
            return self.general(question, sid, decision.reason)

        if intent == "evidence":
            intent = "summary"

        try:
            out = getattr(self, f"do_{intent}")(sid)
        except tools.ToolError as error:
            return response("refused", shipment_id=sid, answer=str(error), tools_used=self.used)

        out.update(shipment_id=sid, tools_used=self.used, citations=self.citations)

        note = self.corrected_note()

        if note and note not in out["uncertainty"]:
            out["uncertainty"] = [*out["uncertainty"], note]

        if self.corrections_unavailable and UNAVAILABLE_NOTE not in out["uncertainty"]:
            out["uncertainty"] = [*out["uncertainty"], UNAVAILABLE_NOTE]

        return out

    def resolve(self, question, shipment_id):
        named = mentioned_shipments(question, self.service)

        if shipment_id:
            chosen = self.service.shipment(shipment_id)

            if chosen is None:
                return response("refused", answer="That shipment is not available.")

            others = [s for s in named if s["shipment_key"] != chosen["shipment_key"]]

            if others:
                return response(
                    "refused", shipment_id=chosen["shipment_id"],
                    answer=(f"This question names {others[0]['shipment_id']}, but the conversation is limited to "
                            f"{chosen['shipment_id']}. Select that shipment to ask about it."),
                )

            return chosen

        if len(named) == 1:
            return named[0]

        if len(named) > 1:
            return response("clarification", answer="Which shipment? The question names more than one: "
                            + ", ".join(s["shipment_id"] for s in named) + ". Ask about one at a time.")

        return response("clarification", answer="Which shipment is this about? Select one or name it in the question.",
                        next_steps=["Choose a shipment in the assistant's scope selector."])

    def general(self, question, sid, reason=None):
        if reason and reason.startswith("action request"):
            return response("insufficient_evidence", shipment_id=sid, answer=READ_ONLY)

        if self.fallback is None:
            return response("insufficient_evidence", shipment_id=sid,
                            answer="The audit tools cannot answer that. Ask about the invoice, quantity, delivery, "
                                   "missing documents, financial exposure or the overall status.")

        try:
            text = self.fallback(question, sid)
        except Exception:
            text = None

        if not text:
            return response("insufficient_evidence", shipment_id=sid,
                            answer="The question is outside the audit tools and the language model is unavailable.")

        return response("answered", shipment_id=sid, answer=text, generated=True,
                        uncertainty=["Generated from the shipment's documents; not a verified audit finding."])

    # ----------------------------------------------------------

    def do_summary(self, sid):
        r = self.call("get_reconciliation_result", shipment_id=sid)

        if not r["reconciled"]:
            return response("insufficient_evidence", answer=f"{sid} could not be reconciled: {r['error']}",
                            uncertainty=[r["error"] or "Reconciliation failed."])

        for audit in tools.AUDITS:
            self.cite(audit, sid)

        status = r["final_status"]
        findings = [{"label": d["category"], "value": d["description"], "status": "discrepancy"} for d in r["discrepancies"]]
        unc = [f"{i['category']}: {i['reason']}" for i in r["review_items"]]
        steps = []

        if status == "NO DISCREPANCIES":
            answer = f"{sid}: no discrepancies found, and every audited value was verified."
        elif status == "DISCREPANCIES DETECTED":
            answer = f"{sid}: {len(findings)} discrepancy(ies) detected."
            steps.append("Prepare an exception review for each discrepancy (a person must approve any action).")
        else:
            answer = f"{sid} needs review: some values could not be verified, so it is not reported as clean."

        return response("answered", answer=answer, findings=findings, uncertainty=unc, next_steps=steps)

    def do_quantity(self, sid):
        q = self.call("get_reconciliation_result", shipment_id=sid)

        if not q["reconciled"]:
            return self.not_reconciled(sid, q)

        self.cite("quantity", sid)
        q = q["quantity"]

        if q["bol"] is None or q["pod"] is None or q["status"] == "NEEDS REVIEW":
            return response("insufficient_evidence", answer=f"The piece counts for {sid} could not be verified.",
                            findings=[{"label": "BOL pieces", "value": q["bol"] if q["bol"] is not None else NA, "status": "unverified"},
                                      {"label": "POD pieces delivered", "value": q["pod"] if q["pod"] is not None else NA, "status": "unverified"}],
                            uncertainty=[q["reason"] or "A piece count is missing or conflicting."],
                            next_steps=["Check the BOL and POD for a readable piece count."])

        diff = q["difference"]
        word = "short" if diff > 0 else "over" if diff < 0 else "no difference"
        answer = (f"BOL: {q['bol']:,} pieces; POD delivered: {q['pod']:,}. " +
                  (f"{abs(diff):,} piece(s) {word}." if diff else "The counts match."))

        return response("answered", answer=answer, findings=[
            {"label": "BOL pieces", "value": q["bol"], "status": "verified"},
            {"label": "POD pieces delivered", "value": q["pod"], "status": "verified"},
            {"label": "Difference (BOL - POD)", "value": diff, "status": q["status"]}])

    def do_delivery(self, sid):
        d = self.call("get_reconciliation_result", shipment_id=sid)

        if not d["reconciled"]:
            return self.not_reconciled(sid, d)

        self.cite("delivery", sid)
        d = d["delivery"]

        if d["days_late"] is None:
            return response("insufficient_evidence", answer=f"The delivery dates for {sid} could not be verified.",
                            uncertainty=[d["reason"] or "A delivery date is missing or conflicting."],
                            next_steps=["Check the BOL and POD for a readable date."])

        n = d["days_late"]
        timing = "on the expected date" if n == 0 else f"{abs(n)} day(s) {'late' if n > 0 else 'early'}"

        return response("answered", answer=f"Expected {d['expected']}; delivered {d['actual']}: {timing}.",
                        findings=[{"label": "Expected (BOL)", "value": d["expected"], "status": "verified"},
                                  {"label": "Actual (POD)", "value": d["actual"], "status": "verified"},
                                  {"label": "Timing", "value": timing, "status": d["status"]}])

    def do_invoice(self, sid):
        i = self.call("get_reconciliation_result", shipment_id=sid)

        if not i["reconciled"]:
            return self.not_reconciled(sid, i)

        self.cite("invoice", sid)
        i = i["invoice"]

        if i["total"] is None and i["line_item_sum"] is None:
            return response("insufficient_evidence", answer=f"No invoice amount for {sid} could be read.",
                            uncertainty=[i["reason"] or "The invoice total is missing."],
                            next_steps=["Request a readable invoice."])

        # The tools return the total and the sum of the line items, not each charge. Say so when a
        # single charge was asked about, instead of answering with totals as if they were that charge.
        charge_note = ([ONE_CHARGE_UNAVAILABLE] if SINGLE_CHARGE.search(self.question.lower()) else [])

        findings = [{"label": "Stated total", "value": money_text(i["total"]), "status": "verified" if i["total"] is not None else "unverified"},
                    {"label": "Sum of line items", "value": money_text(i["line_item_sum"]), "status": i["status"]}]

        if i["status"] == "NEEDS REVIEW":
            return response("insufficient_evidence", answer=f"The invoice of {sid} could not be fully verified.",
                            findings=findings, uncertainty=[i["reason"] or "A line item is missing or conflicting."])

        if i["status"] == "MISMATCH":
            answer = (f"The stated total {money_text(i['total'])} differs from its line items {money_text(i['line_item_sum'])} "
                      f"by {money_text(i['difference'])}. This is an arithmetic mismatch; it does not show overbilling.")
            steps = ["Ask the carrier to explain or correct the invoice (a person must approve any action)."]
        else:
            answer = f"The stated total {money_text(i['total'])} equals the sum of its line items."
            steps = []

        return response("answered", answer=answer, findings=findings, next_steps=steps, uncertainty=charge_note)

    def do_completeness(self, sid):
        c = self.call("check_document_completeness", shipment_id=sid)

        if not c["known"]:
            return response("insufficient_evidence", answer=f"No usable document names {sid}.")

        # Cite the documents that are present. The page is unknown for a
        # document-level fact, so it stays None rather than being guessed.
        listing = self.call("list_shipment_documents", shipment_id=sid)

        for doc in listing["documents"]:
            row = {"source": doc["filename"], "page": None, "document_type": doc["document_type"],
                   "shipment_id": sid, "text": None}

            if row not in self.citations:
                self.citations.append(row)

        have = [t for t, f in c["by_type"].items() if f]
        findings = [{"label": t, "value": ", ".join(f) if f else "missing", "status": "verified" if f else "missing"}
                    for t, f in c["by_type"].items()]
        unc = list(c["warnings"])

        if c["open_unusable_files"]:
            unc.append(f"{c['open_unusable_files']} unusable file(s) in the store cannot be ruled out as belonging to this shipment.")

        if c["missing"]:
            return response("answered", answer=f"{sid} is missing: {', '.join(c['missing'])}. Present: {', '.join(have) or 'nothing'}.",
                            findings=findings, uncertainty=unc,
                            next_steps=[f"Request the {', '.join(c['missing'])} from the carrier or customer (draft only; a person sends it)."])

        return response("answered", answer=f"{sid} has a BOL, an invoice and a POD.", findings=findings, uncertainty=unc)

    def do_exposure(self, sid):
        f = self.call("get_financial_exposure", shipment_id=sid)

        if not f["known"]:
            return response("insufficient_evidence", answer=f"No invoice figures exist for {sid}.")

        self.cite("invoice", sid)
        cls = f["classification"]

        if cls == "unverifiable":
            under = f["amount_under_review"]
            return response("insufficient_evidence", answer=f"The invoice of {sid} could not be verified"
                            + (f"; its stated total {money_text(under)} is under review." if under is not None else "."),
                            uncertainty=["No exposure figure is given for an unverifiable invoice."])

        if cls == "mismatch":
            answer = (f"Confirmed arithmetic mismatch of {money_text(abs(f['signed_difference']))} "
                      f"(total {money_text(f['stated_total'])} vs line items {money_text(f['line_item_sum'])}). "
                      "It is not proof of overbilling or a recoverable amount.")
        else:
            answer = f"The invoice adds up (total {money_text(f['stated_total'])}); no mismatch."

        unc = [] if f["currency"] else ["The documents state no currency; amounts are as written."]

        return response("answered", answer=answer, uncertainty=unc,
                        findings=[{"label": "Classification", "value": cls, "status": cls},
                                  {"label": "Stated total", "value": money_text(f["stated_total"]), "status": "verified"}])

    def not_reconciled(self, sid, r):
        return response("insufficient_evidence", answer=f"{sid} could not be reconciled: {r['error']}",
                        uncertainty=[r["error"] or "Reconciliation failed."])
