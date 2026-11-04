"""#175 M11: the codes that shared machinery returns are documented contract-wide.

``status_code_conformance`` (now on in the fuzzer) fails on any status code an operation does not
document. 401, 403, 429 and 400 come from shared dependencies (the principal, the role gates and
the CSRF guard, the limiters, JSON body parsing), so they are documented from the dependency tree
(``core.errors.document_problem_responses``) and checked here on the committed contract.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from pbip_api.core.errors import document_problem_responses
from pbip_api.main import ROUTERS
from pbip_api.modules.auth.dependencies import Principal, get_current_principal, require_roles
from pbip_api.modules.auth.models import RoleName
from pydantic import BaseModel

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[4] / "docs" / "api" / "openapi.json"
PUBLIC = {"/api/health/live", "/api/health/ready", "/api/v1/auth/register", "/api/v1/auth/login",
          "/api/v1/auth/mfa/verify", "/api/v1/auth/refresh", "/api/v1/auth/logout"}  # fmt: skip


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    return [
        (method.upper(), path, operation)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
    ]


def test_every_authenticated_operation_documents_401() -> None:
    missing = [
        f"{m} {p}" for m, p, op in _operations() if p not in PUBLIC and "401" not in op["responses"]
    ]
    assert missing == []


def test_every_staff_and_customer_only_operation_documents_403() -> None:
    gated = set()
    for router in ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute):
                stack, names = [route.dependant], set()
                while stack:
                    dependant = stack.pop()
                    names.add(getattr(dependant.call, "__name__", ""))
                    stack.extend(dependant.dependencies)
                if names & {"get_current_customer", "require_roles_dependency", "csrf_guard"}:
                    gated |= {f"{method} {route.path}" for method in route.methods}
    assert len(gated) > 50  # the walker found the customer and staff routes
    missing = [f"{m} {p}" for m, p, op in _operations() if f"{m} {p}" in gated
               and "403" not in op["responses"]]  # fmt: skip
    assert missing == []


@pytest.mark.parametrize(
    "operation",
    ["POST /api/v1/auth/login", "POST /api/v1/auth/register", "POST /api/v1/auth/password-change",
     "POST /api/v1/events/batch", "POST /api/v1/assistant/chat", "PUT /api/v1/customers/me/phone"],
)  # fmt: skip
def test_rate_limited_operations_document_429(operation: str) -> None:
    found = {f"{m} {p}": op for m, p, op in _operations()}
    assert "429" in found[operation]["responses"]


def test_every_operation_with_a_json_body_documents_400() -> None:
    missing = [f"{m} {p}" for m, p, op in _operations() if "requestBody" in op
               and "400" not in op["responses"]]  # fmt: skip
    assert missing == []


class _Body(BaseModel):
    amount: int


def test_the_documentation_follows_the_dependencies_and_keeps_a_routes_own_entry() -> None:
    router = APIRouter()
    staff = require_roles(RoleName.ADMIN, mfa=True)

    @router.get("/open")
    def open_route() -> None: ...

    @router.get("/mine")
    def mine(_p: Principal = Depends(get_current_principal)) -> None: ...

    @router.post("/staff", responses={403: {"description": "own"}})
    def staff_route(body: _Body, _p: Principal = Depends(staff)) -> None: ...

    document_problem_responses(router)
    routes = {r.path: r.responses for r in router.routes if isinstance(r, APIRoute)}
    assert routes["/open"] == {}
    assert set(routes["/mine"]) == {401}
    assert set(routes["/staff"]) == {400, 401, 403}
    assert routes["/staff"][403] == {"description": "own"}
