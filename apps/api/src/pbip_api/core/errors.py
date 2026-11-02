"""RFC 7807 problem+json error responses.

Error bodies never echo request input (it may contain passwords or card data) and never
contain stack traces. Every error carries the request id so support can find the log line.
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from pbip_api.core.metrics import UNHANDLED_ERRORS_TOTAL
from pbip_api.core.middleware import route_template

PROBLEM_JSON = "application/problem+json"

_logger = structlog.get_logger(__name__)


class DomainError(Exception):
    """Base class for expected, user-facing business errors raised by services."""

    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers


class NotFoundError(DomainError):
    """Resource missing OR not owned by the caller - the two are deliberately indistinguishable."""

    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(DomainError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class UnprocessableError(DomainError):
    status_code = 422
    code = "unprocessable"


class RateLimitedError(DomainError):
    status_code = 429
    code = "rate_limited"


def _request_id(request: Request) -> str | None:
    value = request.scope.get("pbip_request_id")
    return value if isinstance(value, str) else None


def problem(
    request: Request,
    status: int,
    code: str,
    title: str,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://pbip.example/problems/{code}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": _request_id(request),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        raise exc
    return problem(request, exc.status_code, exc.code, exc.detail, headers=exc.headers)


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    return problem(request, exc.status_code, f"http_{exc.status_code}", str(exc.detail))


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [
        {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    return problem(request, 422, "validation_error", "Request validation failed", errors)


def _raised_at(exc: BaseException) -> str:
    """`module.py:line:function` of the innermost frame: actionable, and carries no data values."""
    tb = exc.__traceback__
    if tb is None:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    return f"{code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno}:{code.co_name}"


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Error tracking (TASK-103): one searchable log event + a metric per unhandled error.

    The request id is bound by RequestContextMiddleware. Only the exception TYPE and code location
    are recorded - never ``str(exc)``, arguments or the request, which may contain personal data.
    """
    route = route_template(request.scope)
    error_type = type(exc).__name__
    _logger.error(
        "unhandled_exception",
        error_type=error_type,
        route=route,
        method=request.method,
        raised_at=_raised_at(exc),
    )
    UNHANDLED_ERRORS_TOTAL.labels(route, error_type).inc()
    return problem(request, 500, "internal_error", "Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
