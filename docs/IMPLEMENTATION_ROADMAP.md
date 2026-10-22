# Implementation roadmap

Status is from inspecting the repository (branch `feature/production-readiness`,
uncommitted). Streams are delivered one at a time.

## Implemented (verified by tests)
- PDF pipeline, shipment-scoped RAG, reconciliation, freight audit, exports.
- Sign-in, roles, exception workflow with hash-chained history, audit history
  and trends, reliable ingestion, FastAPI (~40 endpoints), React (9 screens).

## Stream 1 - AI Logistics Assistant (this stream)
- [x] Read-only validated tools: reconciliation, shipment evidence, document
      completeness, financial exposure, document list (`src/assistant_tools.py`).
- [x] Deterministic orchestration, citations, insufficient-evidence
      responses, shipment isolation (`src/assistant.py`).
- [x] `POST /api/assistant`, `GET /api/assistant/tools`.
- [x] React Assistant screen with sources and uncertainty.
- [x] Tests: answers, unsupported questions, cross-shipment leakage, invalid
      tool arguments, unauthorized access.

## Known gaps (later streams, highest value first)
1. Structured extraction with field confidence and human correction.
2. Evaluation suite (extraction, RAG, agent) with a repeatable command.
3. Browser QA of the React app; rate limiting on the API; CSP review.
4. n8n webhooks/notification adapter; LangGraph only when multi-step approval
   flows need checkpoints; MCP only if other clients need the tools.
5. LLM provider abstraction (hosted provider is opt-in, never default).

## Risks noted
- `/api/chat` (free RAG) predates the assistant and is not tool-bounded; the
  assistant is the supported path.
- Bundle is one 829 kB chunk; split routes when measured to matter.
