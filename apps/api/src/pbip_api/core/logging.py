"""Structured JSON logging with mandatory redaction of sensitive data.

Nothing sensitive may ever reach a log line (AGENTS.md section 6). Redaction runs as the
last structlog processor so it also covers values interpolated by other processors.
"""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from opentelemetry import trace

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "otp",
    "totp",
    "mfa_code",
    "recovery_code",
    "api_key",
    "apikey",
    "card_number",
    "cvv",
    "account_number",
    "iban",
    "ssn",
    "aadhaar",
    "pan_number",
    "private_key",
    "encryption_key",
    # Customer PII (threat model A09 / UK GDPR Art. 25): nothing logs these today; this is the
    # backstop so a future log call cannot leak them by key (#145).
    "full_name",
    "date_of_birth",
    "phone",
)

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_DIGITS_RE = re.compile(r"\b\d{12,19}\b")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _scrub_string(value: str) -> str:
    value = _JWT_RE.sub("[JWT]", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _EMAIL_RE.sub("[EMAIL]", value)
    return _LONG_DIGITS_RE.sub("[NUMBER]", value)


def redact(value: Any, key: str | None = None) -> Any:
    """Return a copy of ``value`` with sensitive keys and patterns removed."""
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def _opentelemetry_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Add the active trace_id/span_id so a log line can be joined to its trace (TASK-085).

    Outside a span, or with tracing disabled (the API returns an invalid no-op span), nothing
    is added. Runs before redaction like every other processor.
    """
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


def _redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {key: redact(value, key) for key, value in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper(), force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _opentelemetry_processor,
            _redaction_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
