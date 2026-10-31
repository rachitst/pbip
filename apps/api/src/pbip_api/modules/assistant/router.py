"""Assistant HTTP route (TASK-057/058). Customer-only; the body is text, never an identifier."""

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from pbip_api.modules.assistant.dependencies import assistant_rate_limit, get_assistant_service
from pbip_api.modules.assistant.schemas import ChatRequest, ChatResponse
from pbip_api.modules.assistant.service import AssistantService
from pbip_api.modules.auth.dependencies import Principal, get_clock, get_current_principal
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


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
