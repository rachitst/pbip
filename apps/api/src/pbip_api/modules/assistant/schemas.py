"""HTTP shapes for the assistant. The request carries text only - never an identifier."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pbip_api.modules.assistant.tools import ToolName

MAX_MESSAGE_CHARS = 500


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ChatResponse(BaseModel):
    reply: str = Field(description="Written by the server from the tool's structured result")
    outcome: Literal["answered", "refused", "help", "unavailable"]
    tool: ToolName | None = Field(description="The approved tool that ran, if any")
    engine: Literal["llm", "rules"] = Field(
        description="llm: Ollama chose the tool; rules: the deterministic fallback did"
    )
    link: str | None = Field(default=None, description="Where in the app to see or act on this")


class AssistantMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(description="Stored after redaction of account numbers, tokens, emails")
    tool: ToolName | None
    created_at: datetime


class AssistantHistory(BaseModel):
    items: list[AssistantMessageOut]


class StreamToolCall(BaseModel):
    """``event: tool_call`` - the router chose a tool; it is about to run."""

    tool: ToolName
    engine: Literal["llm", "rules"]


class StreamToolResult(BaseModel):
    """``event: tool_result`` - ok, input (answered with guidance) or error."""

    tool: ToolName
    outcome: Literal["ok", "input", "error"]


class StreamToken(BaseModel):
    """``event: token`` - the next chunk of the reply."""

    text: str


class StreamError(BaseModel):
    """``event: error`` - the stream failed; generic text only."""

    detail: str


class AssistantStreamFrames(BaseModel):
    """Documentation of the SSE stream: each frame's ``event:`` names one of these fields and its
    ``data:`` line is that field's JSON. ``done`` carries the full ChatResponse."""

    tool_call: StreamToolCall | None = None
    tool_result: StreamToolResult | None = None
    token: StreamToken | None = None
    done: ChatResponse | None = None
    error: StreamError | None = None
