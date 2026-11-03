"""Wiring for the assistant: the chat model (or none) and the per-customer rate limit."""

import structlog
from fastapi import Depends
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.ratelimit import consume, within_budget
from pbip_api.core.redis import get_redis
from pbip_api.modules.assistant.faq import FaqFacts
from pbip_api.modules.assistant.llm import ChatModel, OllamaChatModel
from pbip_api.modules.assistant.providers import (
    Cooldown,
    GroqChatModel,
    ModelChain,
    NamedChatModel,
)
from pbip_api.modules.assistant.service import AssistantService
from pbip_api.modules.auth.dependencies import Principal, get_current_principal
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.runtime_config import service as runtime_config

_WINDOW_SECONDS = 60
# One per API process: after a 429 every request in this process skips Groq for a while.
_GROQ_COOLDOWN = Cooldown()
_logger = structlog.get_logger(__name__)


def get_chat_model(
    settings: Settings = Depends(get_settings),
    customer: Customer = Depends(get_current_customer),
    redis_client: Redis = Depends(get_redis),
) -> ChatModel | None:
    """``None`` when the LLM is switched off: the deterministic rules answer every message.

    Otherwise a chain: Groq first when ``LLM_PROVIDER=groq`` (ADR-0037), then Ollama.
    """
    if not settings.assistant_llm_enabled:
        return None
    models: list[NamedChatModel] = []
    if settings.llm_provider == "groq":
        groq = _groq_model(settings, customer, redis_client)
        if groq is not None:
            models.append(groq)
    models.append(
        OllamaChatModel(
            settings.ollama_base_url,
            settings.ollama_model,
            settings.ollama_timeout_seconds,
            keep_alive=settings.ollama_keep_alive,
        )
    )
    return ModelChain(models)


def _groq_model(
    settings: Settings, customer: Customer, redis_client: Redis
) -> GroqChatModel | None:
    key = settings.groq_api_key.get_secret_value()
    if not key or not settings.groq_model:  # deployed environments refuse to boot like this
        _logger.warning("assistant_groq_not_configured")
        return None
    limit = settings.rate_limit_groq_per_minute
    subject = f"customer:{customer.id}"

    def budget() -> bool:
        return within_budget(redis_client, "assistant_groq", subject, limit, _WINDOW_SECONDS)

    name_parts = customer.full_name.split()
    return GroqChatModel(
        key,
        settings.groq_model,
        settings.groq_timeout_seconds,
        reasoning_effort=settings.groq_reasoning_effort,
        private_terms=[customer.full_name, *name_parts],
        budget=budget,
        cooldown=_GROQ_COOLDOWN,
    )


def get_faq_facts(
    session: Session = Depends(get_session), settings: Settings = Depends(get_settings)
) -> FaqFacts:
    """The limits the FAQ quotes (TASK-105): the values the services enforce right now, including
    an ADMIN's runtime overrides (ADR-0031), not the environment defaults."""
    return FaqFacts.from_settings(runtime_config.effective_settings(session, settings))


def get_assistant_service(
    session: Session = Depends(get_session),
    model: ChatModel | None = Depends(get_chat_model),
    facts: FaqFacts = Depends(get_faq_facts),
) -> AssistantService:
    return AssistantService(session, model, facts)


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
