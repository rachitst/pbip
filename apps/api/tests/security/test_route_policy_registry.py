"""Authorization-by-default guard.

Every route must either be explicitly allow-listed as public, or depend on the authentication
dependency. A new endpoint added without authentication FAILS THIS TEST. This is the
safety net behind object-level authorization (docs/PROJECT_BLUEPRINT.md section 6).
"""

import pytest
from fastapi.routing import APIRoute
from pbip_api.main import ROUTERS
from pbip_test_support import build_app

pytestmark = pytest.mark.security

# Only add a path here with a written justification in the PR description.
PUBLIC_ROUTES: frozenset[str] = frozenset(
    {
        "/api/health/live",  # liveness probe
        "/api/health/ready",  # readiness probe
        "/metrics",  # scraped on the internal Docker network only; never proxied
        "/api/v1/auth/register",  # self-registration (rate limited, off in production)
        "/api/v1/auth/login",  # credential exchange (rate limited, lockout)
        "/api/v1/auth/mfa/verify",  # second step of login; guarded by a short-lived challenge token
        "/api/v1/auth/refresh",  # cookie-authenticated + CSRF guard
        "/api/v1/auth/logout",  # cookie-authenticated + CSRF guard
    }
)

AUTH_DEPENDENCY_NAME = "get_current_principal"


def _dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            names.add(getattr(dependant.call, "__name__", type(dependant.call).__name__))
        stack.extend(dependant.dependencies)
    return names


def _all_api_routes() -> list[APIRoute]:
    routes = [r for router in ROUTERS for r in router.routes if isinstance(r, APIRoute)]
    routes += [r for r in build_app().routes if isinstance(r, APIRoute)]
    return routes


def test_there_are_routes_to_check() -> None:
    assert len(_all_api_routes()) > 20  # guards against the walker silently checking nothing


def test_every_route_is_public_by_allow_list_or_authenticated() -> None:
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in _all_api_routes()
        if route.path not in PUBLIC_ROUTES and AUTH_DEPENDENCY_NAME not in _dependency_names(route)
    ]
    assert not offenders, f"Routes without authentication and not allow-listed: {offenders}"


def test_public_allow_list_has_no_stale_entries() -> None:
    existing = {route.path for route in _all_api_routes()}
    assert existing >= PUBLIC_ROUTES, f"Stale allow-list entries: {PUBLIC_ROUTES - existing}"
