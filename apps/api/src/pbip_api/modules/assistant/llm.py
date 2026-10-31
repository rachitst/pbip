"""LLM adapter and router (TASK-058): Ollama with grammar-constrained JSON function calling.

The model has exactly one job: pick an approved tool and its arguments. Its output is then
validated twice - by Ollama's JSON-schema grammar (the model cannot emit other keys or tools)
and by the tool's own Pydantic model on the server. It never sees tool results or retrieved
text, and it never writes the answer: the server renders that from structured data, so figures
cannot be hallucinated and a poisoned merchant name has no model to instruct.

The base URL is fixed configuration (SSRF: never a model- or user-chosen URL).
"""

import json
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from pbip_api.modules.assistant.fallback import Route
from pbip_api.modules.assistant.tools import REGISTRY, ToolName

NO_TOOL = "none"


class LLMUnavailableError(Exception):
    """Ollama unreachable, too slow, or answered with something unusable. Caller falls back."""


class ChatModel(Protocol):
    def complete_json(self, system: str, user: str, schema: Mapping[str, object]) -> str:
        """Return the model's JSON text for this system prompt, message and output schema."""
        ...


class OllamaChatModel:
    """Minimal client for Ollama's ``POST /api/chat`` with ``format=<json schema>``."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/api/chat"
        self._model = model
        self._timeout = timeout_seconds
        self._transport = transport

    def complete_json(self, system: str, user: str, schema: Mapping[str, object]) -> str:
        body = {
            "model": self._model,
            "stream": False,
            "format": schema,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 96},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(self._url, json=body)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailableError(type(exc).__name__) from exc
        content = payload.get("message", {}).get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise LLMUnavailableError("no message content")
        return content


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$ref``s and forbid extra keys, so the grammar matches the server-side model."""
    # Any: JSON Schema is an arbitrary JSON document produced by pydantic.
    defs: dict[str, Any] = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return walk(dict(defs[ref.removeprefix("#/$defs/")]))
            out = {k: walk(v) for k, v in node.items() if k not in ("title", "description")}
            if out.get("type") == "object":
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


def output_schema() -> dict[str, object]:
    """One branch per approved tool (``tool`` const + that tool's argument schema), plus none."""
    branches: list[dict[str, object]] = [
        {
            "type": "object",
            "properties": {
                "tool": {"const": tool.name.value},
                "arguments": strict_schema(tool.args_model.model_json_schema()),
            },
            "required": ["tool", "arguments"],
            "additionalProperties": False,
        }
        for tool in REGISTRY.values()
    ]
    branches.append(
        {
            "type": "object",
            "properties": {
                "tool": {"const": NO_TOOL},
                "arguments": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            "required": ["tool", "arguments"],
            "additionalProperties": False,
        }
    )
    return {"anyOf": branches}


class _Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: ToolName | None
    arguments: dict[str, object]


class InvalidChoiceError(ValueError):
    """The model's output is not valid JSON, names no approved tool, or breaks a bound."""


def parse_choice(text: str) -> Route:
    """Validate the model's JSON against the approved tool's own argument model."""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("tool") == NO_TOOL:
            data = {**data, "tool": None}
        choice = _Choice.model_validate(data)
        if choice.tool is None:
            if choice.arguments:
                raise InvalidChoiceError("arguments given with no tool")
            return Route(None)
        REGISTRY[choice.tool].args_model.model_validate(choice.arguments)  # bounds, no extra keys
    except (ValueError, ValidationError) as exc:  # JSONDecodeError and ValidationError included
        raise InvalidChoiceError(type(exc).__name__) from exc
    return Route(choice.tool, choice.arguments)


def llm_route(model: ChatModel, system: str, message: str) -> Route:
    """Ask the model for a tool choice. Raises LLMUnavailableError for anything unusable."""
    text = model.complete_json(system, message, output_schema())
    try:
        return parse_choice(text)
    except InvalidChoiceError as exc:
        raise LLMUnavailableError("invalid tool choice") from exc
