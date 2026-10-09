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


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    _logger.error("unhandled_exception", error_type=type(exc).__name__)
    return problem(request, 500, "internal_error", "Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
