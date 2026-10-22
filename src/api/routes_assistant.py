"""HTTP API: the AI Logistics Assistant (see src/assistant.py)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import assistant
import assistant_tools
import authz
from api.core import ApiState, get_state, require

router = APIRouter(prefix="/api/assistant")


class AskBody(BaseModel):

    question: str = Field(min_length=1, max_length=1000)
    shipment_id: str | None = Field(default=None, max_length=64)


def rag_fallback(question, shipment_id):
    # Imported late: it needs the index and the language model
    from rag import ask_question

    answer, _ = ask_question(question, shipment_id)

    return answer


@router.get("/tools")
def tools(principal=Depends(require(authz.EXCEPTIONS_VIEW))):

    return assistant_tools.describe_tools()


@router.post("")
def ask(body: AskBody, principal=Depends(require(authz.EXCEPTIONS_VIEW)), state: ApiState = Depends(get_state)):
    """
    Answers about one authorized shipment from read-only audit tools. A
    question about another shipment than the chosen one is refused.
    """

    return assistant.Assistant(state.service, principal, fallback=rag_fallback).ask(body.question, body.shipment_id)
