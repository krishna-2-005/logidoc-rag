"""
LogiDoc-RAG assistant tools

The only things the AI Logistics Assistant can do: a fixed registry of
read-only functions over the existing audit service. Each tool

    - is looked up by name (an unknown name is refused)
    - takes exactly its declared arguments, validated for type and length
      (an extra, missing or malformed argument is refused)
    - is checked against the caller's permission and against the shipment
      scope the request was authorized for: a tool cannot read another
      shipment, whatever the arguments say
    - returns plain data computed by the existing modules; no arithmetic or
      access decision is made by a language model, and nothing is written.

There is no tool for SQL, shell, files, URLs or workflow changes.
"""

import authz
import financial_exposure as exposure
from shipment_identity import identifier_key

AUDITS = ("quantity", "delivery", "invoice")
MAX_ID = 64


class ToolError(ValueError):
    """A tool call that is refused: unknown tool, bad arguments, or out of scope."""


class Scope:
    """What one request may read: a single shipment key, or none chosen yet."""

    def __init__(self, shipment_key=None):
        self.shipment_key = shipment_key


def check_shipment_arg(value, scope):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID:
        raise ToolError("shipment_id must be a non-empty text of at most 64 characters.")

    key = identifier_key(value)

    if not key:
        raise ToolError("shipment_id is not a shipment identifier.")

    if scope.shipment_key is None or key != scope.shipment_key:
        raise ToolError("That shipment is outside the scope this request is authorized for.")

    return key


def shipment_of(service, key):
    found = service.shipment(key)

    if found is None:
        raise ToolError("No such shipment.")

    return found


def clean_evidence(items, key):
    """Evidence rows of this shipment only, and how many of another were dropped."""

    rows, foreign = [], 0

    for item in items or []:
        if identifier_key(item.get("shipment_id")) != key:
            foreign += 1
            continue

        rows.append({
            "source": item.get("source"), "page": item.get("page"),
            "document_type": item.get("document_type"), "shipment_id": item.get("shipment_id"),
            "text": str(item.get("text") or "")[:300],
            # "legacy_reader" (the document's own value) or "approved_correction" (a reviewer-approved
            # value: the page is then unknown and the document does not state it)
            "value_origin": item.get("value_origin") or "legacy_reader", "correction_id": item.get("correction_id"),
        })

    return rows, foreign


def reconciliation(service, key):
    shipment = shipment_of(service, key)
    result = shipment["result"]

    if not result.get("success"):
        return {"shipment_id": shipment["shipment_id"], "reconciled": False, "error": result.get("error")}

    return {
        "shipment_id": shipment["shipment_id"], "reconciled": True, "final_status": result.get("final_status"),
        "quantity": {k: result["quantity"].get(k) for k in ("bol", "pod", "difference", "status", "reason")},
        "delivery": {k: result["delivery"].get(k) for k in ("expected", "actual", "days_late", "status", "reason")},
        "invoice": {k: result["invoice"].get(k) for k in ("total", "line_item_sum", "difference", "status", "reason")},
        "discrepancies": result.get("discrepancies") or [],
        "review_items": result.get("review_items") or [],
        # Values this result took from reviewer-approved corrections instead of the document
        "corrections_unavailable": bool((shipment.get("inputs") or {}).get("corrections_unavailable")),
        "corrections_applied": [
            {"field": f["field"], "correction_id": f["correction_id"], "document": f["document"]}
            for f in (shipment.get("inputs") or {}).get("fields", []) if f["source"] == "approved_correction" and f["resolved"]
        ],
    }


def evidence(service, key, audit):
    if audit not in AUDITS:
        raise ToolError(f"audit must be one of {list(AUDITS)}.")

    shipment = shipment_of(service, key)
    result = shipment["result"]

    if not result.get("success"):
        return {"shipment_id": shipment["shipment_id"], "audit": audit, "evidence": [], "excluded_foreign": 0}

    rows, foreign = clean_evidence((result.get(audit) or {}).get("evidence"), key)

    return {"shipment_id": shipment["shipment_id"], "audit": audit, "evidence": rows, "excluded_foreign": foreign}


def completeness(service, key):
    shipment = shipment_of(service, key)
    row = next((r for r in service.completeness()["shipments"] if r["shipment_key"] == key), None)

    if row is None:
        return {"shipment_id": shipment["shipment_id"], "known": False}

    return {
        "shipment_id": row["shipment_id"], "known": True, "readiness": row["readiness"], "missing": row["missing"],
        "by_type": row["by_type"], "warnings": row["warnings"], "next_step": row["next_step"],
        "open_unusable_files": len(service.completeness()["unusable"]),
    }


def financial(service, key):
    shipment = shipment_of(service, key)
    rows = [r for r in service.exposure_rows() if r["shipment_key"] == key]

    if not rows:
        return {"shipment_id": shipment["shipment_id"], "known": False}

    row = rows[0]

    return {
        "shipment_id": shipment["shipment_id"], "known": True, "classification": row["classification"],
        "stated_total": row["stated_total"], "line_item_sum": row["line_item_sum"],
        "signed_difference": row["signed_difference"], "amount_under_review": row["amount_under_review"],
        "currency": row["currency"], "note": exposure.NOTE,
    }


def documents(service, key):
    shipment_of(service, key)

    return {
        "shipment_id": shipment_of(service, key)["shipment_id"],
        "documents": [
            {k: f[k] for k in ("filename", "document_type", "status", "total_pages", "text_pages")}
            for f in service.scan() if f["shipment_key"] == key
        ],
    }


# name -> (function, {argument: validator})
REGISTRY = {
    "get_reconciliation_result": (reconciliation, ("shipment_id",)),
    "get_shipment_evidence": (evidence, ("shipment_id", "audit")),
    "check_document_completeness": (completeness, ("shipment_id",)),
    "get_financial_exposure": (financial, ("shipment_id",)),
    "list_shipment_documents": (documents, ("shipment_id",)),
}


def describe_tools():
    return [{"name": name, "arguments": list(args), "read_only": True} for name, (_, args) in REGISTRY.items()]


def run_tool(principal, service, scope, name, arguments):
    """Runs one tool for an authorized principal inside the request's scope."""

    authz.require(principal, authz.EXCEPTIONS_VIEW)

    if name not in REGISTRY:
        raise ToolError(f"Unknown tool: {str(name)[:40]!r}.")

    function, allowed = REGISTRY[name]

    if not isinstance(arguments, dict):
        raise ToolError("Tool arguments must be an object.")

    extra, missing = set(arguments) - set(allowed), set(allowed) - set(arguments)

    if extra or missing:
        raise ToolError(f"Arguments must be exactly {list(allowed)}.")

    key = check_shipment_arg(arguments["shipment_id"], scope)

    if name == "get_shipment_evidence":
        return function(service, key, arguments["audit"])

    return function(service, key)
