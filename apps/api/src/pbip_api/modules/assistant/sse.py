"""Server-Sent Events framing for the assistant stream (TASK-060)."""

import json
import re
from collections.abc import Iterator

_CHUNK = re.compile(r"\S+\s*")


def format_event(name: str, data: object) -> str:
    """One SSE frame. ``data`` is JSON on a single line, so a newline in text cannot end it."""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def reply_chunks(reply: str) -> Iterator[str]:
    """The reply word by word (with its trailing space), for progressive display."""
    yield from _CHUNK.findall(reply)
