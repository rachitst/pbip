"""HTTP shapes for the assistant. The request carries text only - never an identifier."""

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
