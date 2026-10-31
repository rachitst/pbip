"""Assistant HTTP routes (TASK-057..060). Customer-only; bodies carry text, never identifiers."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Engine

from pbip_api.core.db import get_engine
from pbip_api.modules.assistant.dependencies import (
    assistant_rate_limit,
    get_assistant_service,
    get_chat_model,
)
from pbip_api.modules.assistant.llm import ChatModel
from pbip_api.modules.assistant.schemas import (
    AssistantHistory,
    AssistantStreamFrames,
    ChatRequest,
    ChatResponse,
)
from pbip_api.modules.assistant.service import AssistantService, stream_chat
from pbip_api.modules.auth.dependencies import Principal, get_clock, get_current_principal
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

# Any: FastAPI types OpenAPI response overrides as dict[int | str, dict[str, Any]].
_SSE_DOC: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "text/event-stream: tool_call, tool_result, token..., then done (or error)."
        " Frame payloads are documented by AssistantStreamFrames.",
        "model": AssistantStreamFrames,
        "content": {
            "text/event-stream": {
                # One SSE frame: `event:` name and a single JSON `data:` line.
                "schema": {
                    "type": "object",
                    "properties": {
                        "event": {
                            "type": "string",
                            "enum": ["tool_call", "tool_result", "token", "done", "error"],
                        },
                        "data": {"type": "string", "description": "JSON payload"},
                    },
                    "required": ["event", "data"],
                }
            }
        },
    }
}


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Ask the read-only banking assistant about your own accounts",
)
def chat(
    body: ChatRequest,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    service: AssistantService = Depends(get_assistant_service),
    clock: Callable[[], float] = Depends(get_clock),
    _limited: None = Depends(assistant_rate_limit),
) -> ChatResponse:
    """Identity comes from the access token; tools only read the caller's own data."""
    return service.chat(
        customer.id, principal.user_id, body.message, datetime.fromtimestamp(clock(), tz=UTC)
    )


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    responses=_SSE_DOC,
    summary="The same assistant as /chat, streamed as Server-Sent Events",
)
def chat_stream(
    body: ChatRequest,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    engine: Engine = Depends(get_engine),
    model: ChatModel | None = Depends(get_chat_model),
    clock: Callable[[], float] = Depends(get_clock),
    _limited: None = Depends(assistant_rate_limit),
) -> StreamingResponse:
    events = stream_chat(
        engine,
        model,
        customer.id,
        principal.user_id,
        body.message,
        datetime.fromtimestamp(clock(), tz=UTC),
    )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/messages",
    response_model=AssistantHistory,
    summary="Your own recent assistant conversation (redacted)",
)
def history(
    limit: int = Query(default=50, ge=1, le=100),
    customer: Customer = Depends(get_current_customer),
    service: AssistantService = Depends(get_assistant_service),
) -> AssistantHistory:
    return service.history(customer.id, limit)
