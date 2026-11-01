"""Wiring for the assistant: the chat model (or none) and the per-customer rate limit."""

from fastapi import Depends
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.ratelimit import consume
from pbip_api.core.redis import get_redis
from pbip_api.modules.assistant.llm import ChatModel, OllamaChatModel
from pbip_api.modules.assistant.service import AssistantService
from pbip_api.modules.auth.dependencies import Principal, get_current_principal

_WINDOW_SECONDS = 60


def get_chat_model(settings: Settings = Depends(get_settings)) -> ChatModel | None:
    """``None`` when the LLM is switched off: the deterministic rules answer every message."""
    if not settings.assistant_llm_enabled:
        return None
    return OllamaChatModel(
        settings.ollama_base_url,
        settings.ollama_model,
        settings.ollama_timeout_seconds,
        keep_alive=settings.ollama_keep_alive,
    )


def get_assistant_service(
    session: Session = Depends(get_session),
    model: ChatModel | None = Depends(get_chat_model),
) -> AssistantService:
    return AssistantService(session, model)


def assistant_rate_limit(
    principal: Principal = Depends(get_current_principal),
    redis_client: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> None:
    """Per signed-in user, not per IP: each message can cost seconds of model time."""
    consume(
        redis_client,
        "assistant",
        f"user:{principal.user_id}",
        settings.rate_limit_assistant_per_minute,
        _WINDOW_SECONDS,
    )
