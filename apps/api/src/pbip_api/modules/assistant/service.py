"""Assistant use case (TASK-057/058): guard -> route (LLM or rules) -> approved tool -> reply.

Pipeline, in order:

1. :mod:`guard` refuses money movement, other people's data, SQL and instruction overrides
   before any model sees the message.
2. The LLM router picks a tool (grammar-constrained JSON, validated again here). If Ollama is
   off, slow or wrong, the deterministic :mod:`fallback` router answers instead.
3. The tool runs in a ``READ ONLY`` transaction with the customer id from the access token.
4. The server writes the reply from the tool's structured result.
5. One audit row (tool name or refusal reason, outcome, engine - never the message or the
   result) is committed in a separate, writable transaction.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from pbip_api.core.errors import DomainError
from pbip_api.modules.assistant import fallback, guard
from pbip_api.modules.assistant.fallback import Route
from pbip_api.modules.assistant.llm import ChatModel, LLMUnavailableError, llm_route
from pbip_api.modules.assistant.prompts import system_prompt
from pbip_api.modules.assistant.schemas import ChatResponse
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
class _Decision:
    route: Route
    engine: Engine


class AssistantService:
    def __init__(self, session: Session, model: ChatModel | None) -> None:
        self.session = session
        self.model = model

    def chat(self, customer_id: UUID, user_id: UUID, message: str, now: datetime) -> ChatResponse:
        started = time.perf_counter()
        refusal = guard.check(message)
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
            decision = self._decide(message)
            response = self._answer(customer_id, user_id, decision, now)
        self.session.commit()
        _logger.info(
            "assistant_chat",
            outcome=response.outcome,
            tool=response.tool.value if response.tool else None,
            engine=response.engine,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        return response

    def _decide(self, message: str) -> _Decision:
        if self.model is not None:
            try:
                return _Decision(llm_route(self.model, system_prompt(), message), "llm")
            except LLMUnavailableError as exc:
                _logger.warning("assistant_llm_fallback", reason=str(exc))
        return _Decision(fallback.route(message), "rules")

    def _answer(
        self, customer_id: UUID, user_id: UUID, decision: _Decision, now: datetime
    ) -> ChatResponse:
        tool = decision.route.tool
        if tool is None:
            return ChatResponse(reply=HELP_TEXT, outcome="help", tool=None, engine=decision.engine)
        reply, outcome = self._run_read_only(
            ToolContext(self.session, customer_id, user_id, now), tool, decision.route.arguments
        )
        self._audit(
            user_id,
            "assistant.tool_call",
            {"tool": tool.value, "outcome": outcome, "engine": decision.engine},
        )
        return ChatResponse(
            reply=reply.text,
            outcome="answered" if outcome in ("ok", "input") else "unavailable",
            tool=tool,
            engine=decision.engine,
            link=reply.link,
        )

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
