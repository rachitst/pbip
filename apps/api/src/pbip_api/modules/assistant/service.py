"""Assistant use case (TASK-057..060): clean -> guard -> route -> approved tool -> reply.

Pipeline, in order:

1. :func:`sanitize.clean_user_message` normalises the text (NFKC, no control or zero-width
   characters, 500 characters max).
2. :mod:`guard` refuses money movement, other people's data, SQL, privilege requests and
   instruction overrides (also inside base64) before any model sees the message.
3. The LLM router picks a tool (grammar-constrained JSON, validated again here). If Ollama is
   off, slow or wrong, the deterministic :mod:`fallback` router answers instead.
4. The tool runs in a ``READ ONLY`` transaction with the customer id from the access token.
5. The server writes the reply from the tool's structured result; :func:`sanitize.redact_output`
   masks account numbers, tokens and emails.
6. In one writable transaction: the redacted exchange is stored (``assistant_messages``) and one
   audit row is written (tool or refusal reason, outcome, engine - never the message or result).

:meth:`AssistantService.run` yields the steps as events, so the JSON endpoint and the SSE stream
(TASK-060) share one implementation.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import Engine as DbEngine
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from pbip_api.core.errors import DomainError
from pbip_api.modules.assistant import fallback, guard, repository
from pbip_api.modules.assistant.fallback import Route
from pbip_api.modules.assistant.llm import ChatModel, LLMUnavailableError, llm_route
from pbip_api.modules.assistant.prompts import system_prompt
from pbip_api.modules.assistant.sanitize import clean_user_message, redact_output
from pbip_api.modules.assistant.schemas import (
    AssistantHistory,
    AssistantMessageOut,
    ChatResponse,
)
from pbip_api.modules.assistant.sse import format_event, reply_chunks
from pbip_api.modules.assistant.tools import (
    ToolContext,
    ToolInputError,
    ToolName,
    ToolReply,
    invoke,
)
from pbip_api.modules.compliance.service import AuditService

_logger = structlog.get_logger(__name__)

HELP_TEXT = (
    "I can tell you your balance, your recent transactions, what you spent this month or last "
    "month (by category too), a predicted balance up to 30 days ahead, and whether a loan is "
    'affordable - for example: "Can I afford a 5 lakh car loan over 5 years?"'
)
UNAVAILABLE_TEXT = "I couldn't get that information right now. Please try again shortly."

Engine = Literal["llm", "rules"]


@dataclass(frozen=True)
class ToolCalled:
    tool: ToolName
    engine: Engine


@dataclass(frozen=True)
class ToolFinished:
    tool: ToolName
    outcome: str


@dataclass(frozen=True)
class Finished:
    response: ChatResponse


ChatEvent = ToolCalled | ToolFinished | Finished


@dataclass(frozen=True)
class _Decision:
    route: Route
    engine: Engine


class AssistantService:
    def __init__(self, session: Session, model: ChatModel | None) -> None:
        self.session = session
        self.model = model

    def chat(self, customer_id: UUID, user_id: UUID, message: str, now: datetime) -> ChatResponse:
        for event in self.run(customer_id, user_id, message, now):
            if isinstance(event, Finished):
                return event.response
        raise RuntimeError("assistant pipeline ended without a reply")  # unreachable by design

    def run(
        self, customer_id: UUID, user_id: UUID, message: str, now: datetime
    ) -> Iterator[ChatEvent]:
        started = time.perf_counter()
        cleaned = clean_user_message(message)
        refusal = guard.check(cleaned)
        if refusal is not None:
            response = ChatResponse(
                reply=guard.REFUSAL_TEXT[refusal],
                outcome="refused",
                tool=None,
                engine="rules",
                link=guard.REFUSAL_LINK[refusal],
            )
            self._audit(user_id, "assistant.refused", {"reason": refusal.value})
        else:
            decision = self._decide(cleaned)
            tool = decision.route.tool
            if tool is None:
                response = ChatResponse(
                    reply=HELP_TEXT, outcome="help", tool=None, engine=decision.engine
                )
            else:
                yield ToolCalled(tool, decision.engine)
                ctx = ToolContext(self.session, customer_id, user_id, now)
                reply, outcome = self._run_read_only(ctx, tool, decision.route.arguments)
                yield ToolFinished(tool, outcome)
                self._audit(
                    user_id,
                    "assistant.tool_call",
                    {"tool": tool.value, "outcome": outcome, "engine": decision.engine},
                )
                response = ChatResponse(
                    reply=redact_output(reply.text),
                    outcome="answered" if outcome in ("ok", "input") else "unavailable",
                    tool=tool,
                    engine=decision.engine,
                    link=reply.link,
                )
        repository.add_exchange(
            self.session, customer_id, redact_output(cleaned), response.reply, response.tool
        )
        self.session.commit()
        _logger.info(
            "assistant_chat",
            outcome=response.outcome,
            tool=response.tool.value if response.tool else None,
            engine=response.engine,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        yield Finished(response)

    def history(self, customer_id: UUID, limit: int) -> AssistantHistory:
        rows = repository.list_messages(self.session, customer_id, limit)
        return AssistantHistory(
            items=[
                AssistantMessageOut(
                    role="user" if row.role == "user" else "assistant",
                    content=row.content,
                    tool=ToolName(row.tool_name) if row.tool_name else None,
                    created_at=row.created_at,
                )
                for row in rows
            ]
        )

    def _decide(self, message: str) -> _Decision:
        if self.model is not None:
            try:
                return _Decision(llm_route(self.model, system_prompt(), message), "llm")
            except LLMUnavailableError as exc:
                _logger.warning("assistant_llm_fallback", reason=str(exc))
        return _Decision(fallback.route(message), "rules")

    def _run_read_only(
        self, ctx: ToolContext, tool: ToolName, arguments: dict[str, object]
    ) -> tuple[ToolReply, str]:
        """Run the tool in its own READ ONLY transaction; PostgreSQL rejects any write."""
        self.session.rollback()  # end the auth lookup's transaction; the next one starts read-only
        self.session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            return invoke(tool, ctx, arguments), "ok"
        except ToolInputError as exc:
            return ToolReply(exc.detail), "input"
        except ValidationError:
            return ToolReply(HELP_TEXT), "input"
        except DomainError as exc:
            # Business answers from the service layer (e.g. not enough history to forecast).
            return ToolReply(exc.detail), "input"
        except Exception as exc:
            _logger.warning("assistant_tool_failed", tool=tool.value, error=type(exc).__name__)
            return ToolReply(UNAVAILABLE_TEXT), "error"
        finally:
            self.session.rollback()

    def _audit(self, user_id: UUID, action: str, details: dict[str, object]) -> None:
        AuditService(self.session).record(user_id, action, "assistant", details=dict(details))


def stream_chat(
    engine: DbEngine,
    model: ChatModel | None,
    customer_id: UUID,
    user_id: UUID,
    message: str,
    now: datetime,
) -> Iterator[str]:
    """Server-Sent Events for one message (TASK-060).

    The stream outlives the request's dependencies, so it owns its session. Events:
    ``tool_call`` when a tool is chosen, ``tool_result`` when it ran, ``token`` chunks of the
    (server-written, redacted) reply, then ``done`` with the full response. Any failure ends the
    stream with a generic ``error`` event - no details, no stack trace.
    """
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        try:
            for event in AssistantService(session, model).run(customer_id, user_id, message, now):
                if isinstance(event, ToolCalled):
                    yield format_event(
                        "tool_call", {"tool": event.tool.value, "engine": event.engine}
                    )
                elif isinstance(event, ToolFinished):
                    yield format_event(
                        "tool_result", {"tool": event.tool.value, "outcome": event.outcome}
                    )
                else:
                    for chunk in reply_chunks(event.response.reply):
                        yield format_event("token", {"text": chunk})
                    yield format_event("done", event.response.model_dump(mode="json"))
        except Exception as exc:
            session.rollback()
            _logger.warning("assistant_stream_failed", error=type(exc).__name__)
            yield format_event("error", {"detail": UNAVAILABLE_TEXT})
