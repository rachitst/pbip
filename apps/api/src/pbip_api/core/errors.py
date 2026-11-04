"""RFC 7807 problem+json error responses.

Error bodies never echo request input (it may contain passwords or card data) and never
contain stack traces. Every error carries the request id so support can find the log line.
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, APIRouter
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
    """`module.py:line:function` of the innermost frame: actionable, and carries no data values.

    Only the file's basename is kept, for both separators: a Windows ``co_filename`` uses ``\\``,
    and the full path would put the developer's or server's directory layout into the log (#329).
    """
    tb = exc.__traceback__
    if tb is None:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{filename}:{tb.tb_lineno}:{code.co_name}"


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


NOT_FOUND_DESCRIPTION = (
    "Not found (problem+json). A resource that exists but is not the caller's is also 404"
)


def document_not_found_responses(router: APIRouter) -> None:
    """Document 404 on every route of ``router`` with a path parameter, unless it has its own.

    Every such route looks its resource up (through a policy function for customer data), and both
    "missing" and "not yours" answer 404, but the contract did not say so (#175). Call it before
    the router is included: FastAPI reads ``route.responses`` when it builds the schema.
    """
    for route in router.routes:
        if not isinstance(route, APIRoute) or not route.dependant.path_params:
            continue
        if 404 not in route.responses and "404" not in route.responses:
            route.responses[404] = {"description": NOT_FOUND_DESCRIPTION}


UNAUTHORIZED_DESCRIPTION = "Missing, invalid or expired access token (problem+json)"
FORBIDDEN_DESCRIPTION = (
    "Not allowed (problem+json): wrong role, a staff route without an MFA session, a customer "
    "route called by a non-customer, or a cookie route without the CSRF header"
)
RATE_LIMITED_DESCRIPTION = "Too many requests in the limiter's window (problem+json)"
MALFORMED_BODY_DESCRIPTION = "The request body is not valid JSON (problem+json)"
# The auth module's checks that answer 403: customer-only routes, the staff ``require_roles``
# gates and the CSRF guard of the cookie routes.
_ROLE_GATES = frozenset({"get_current_customer", "require_roles_dependency", "csrf_guard"})


def _dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        names.add(getattr(dependant.call, "__name__", ""))
        stack.extend(dependant.dependencies)
    return names


def document_problem_responses(router: APIRouter) -> None:
    """Document the codes that shared machinery returns: 401 on authenticated routes, 403 on
    role-gated or CSRF-guarded ones, 429 on rate-limited ones and 400 (unparseable JSON) on
    routes with a body.

    #175 M11: the codes come from shared dependencies, so the contract did not list them and
    ``status_code_conformance`` reported each as undocumented. They are read from the route's
    dependency tree by name (``core`` must not import the auth module): the principal dependency
    and the ``rate_limit*`` / ``*_rate_limit`` limiters. A route's own entry is kept. Call it
    before the router is included, like ``document_not_found_responses``.
    """
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        names = _dependency_names(route)
        codes: dict[int, str] = {}
        if "get_current_principal" in names:
            codes[401] = UNAUTHORIZED_DESCRIPTION
        if names & _ROLE_GATES:
            codes[403] = FORBIDDEN_DESCRIPTION
        if any(name.startswith("rate_limit") or name.endswith("_rate_limit") for name in names):
            codes[429] = RATE_LIMITED_DESCRIPTION
        if route.dependant.body_params:
            codes[400] = MALFORMED_BODY_DESCRIPTION
        for code, description in codes.items():
            if code not in route.responses and str(code) not in route.responses:
                route.responses[code] = {"description": description}


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
