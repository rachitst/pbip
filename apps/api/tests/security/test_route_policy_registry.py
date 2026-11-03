"""Authorization-by-default guard.

Every route must either be explicitly allow-listed as public, or depend on the authentication
dependency. A new endpoint added without authentication FAILS THIS TEST. This is the
safety net behind object-level authorization (docs/PROJECT_BLUEPRINT.md section 6).
"""

import pytest
from fastapi.routing import APIRoute
from pbip_api.main import ROUTERS
from pbip_api.modules.compliance.router import compliance_gate
from pbip_api.modules.fraud.router import analyst_gate
from pbip_api.modules.insights.router import assignment_gate, rm_gate
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


def test_every_fraud_route_is_behind_the_mfa_analyst_gate() -> None:
    """Fraud data (alerts, stats, the security monitoring panels) is staff-only with MFA.

    ``analyst_gate`` is ``require_roles(FRAUD_ANALYST, ADMIN, mfa=True)``; a new fraud route that
    uses a weaker gate (or only authentication) fails here.
    """
    fraud_routes = [r for r in _all_api_routes() if r.path.startswith("/api/v1/fraud/")]
    assert {"/api/v1/fraud/stats", "/api/v1/fraud/monitoring", "/api/v1/fraud/demo-lab"} <= {
        r.path for r in fraud_routes
    }
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in fraud_routes
        if analyst_gate not in {d.call for d in route.dependant.dependencies}
    ]
    assert not offenders, f"Fraud routes without the MFA analyst gate: {offenders}"


# ADR-0032: deciding who looks after a customer is an ADMIN decision, so these routes use the
# stricter ``assignment_gate`` instead of ``rm_gate``. Only add a path here with that reason.
ASSIGNMENT_ROUTES: frozenset[str] = frozenset(
    {
        "/api/v1/insights/relationship-managers",
        "/api/v1/insights/customers/{customer_id}/assignment",
    }
)


def test_every_insights_route_is_behind_the_mfa_rm_gate() -> None:
    """RM insights (churn signals, health, outreach notes about customers) are staff-only with MFA.

    ``rm_gate`` is ``require_roles(RELATIONSHIP_MANAGER, ADMIN, mfa=True)`` (TASK-056, TASK-066);
    a new insights route that uses a weaker gate (or only authentication) fails here. The
    assignment routes are ADMIN-only instead (next test).
    """
    insights_routes = [
        r
        for r in _all_api_routes()
        if r.path.startswith("/api/v1/insights/") and r.path not in ASSIGNMENT_ROUTES
    ]
    assert {
        "/api/v1/insights/customers",
        "/api/v1/insights/behaviour",
        "/api/v1/insights/customers/{customer_id}",
        "/api/v1/insights/customers/{customer_id}/interactions",
    } <= {r.path for r in insights_routes}
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in insights_routes
        if rm_gate not in {d.call for d in route.dependant.dependencies}
    ]
    assert not offenders, f"Insights routes without the MFA RM gate: {offenders}"


def test_rm_assignment_routes_are_admin_only_with_mfa() -> None:
    """Assigning customers to RMs (ADR-0032) is ``require_roles(ADMIN, mfa=True)``: an RM must not
    be able to widen their own portfolio."""
    routes = [r for r in _all_api_routes() if r.path in ASSIGNMENT_ROUTES]
    assert {(next(iter(r.methods)), r.path) for r in routes} == {
        ("GET", "/api/v1/insights/relationship-managers"),
        ("PUT", "/api/v1/insights/customers/{customer_id}/assignment"),
        ("DELETE", "/api/v1/insights/customers/{customer_id}/assignment"),
    }
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if assignment_gate not in {d.call for d in route.dependant.dependencies}
    ]
    assert not offenders, f"Assignment routes without the MFA admin gate: {offenders}"


def test_every_compliance_route_is_behind_the_mfa_compliance_gate() -> None:
    """Audit history and the exceptions dashboard (TASK-048, TASK-100) are compliance work.

    ``compliance_gate`` is ``require_roles(COMPLIANCE_OFFICER, ADMIN, mfa=True)``; a new compliance
    route with a weaker gate (or only authentication) fails here.
    """
    routes = [r for r in _all_api_routes() if r.path.startswith("/api/v1/compliance/")]
    assert {
        "/api/v1/compliance/audit-logs",
        "/api/v1/compliance/exceptions",
        "/api/v1/compliance/exceptions/{kind}/{ref}/acknowledge",
        "/api/v1/compliance/exceptions/{kind}/{ref}/resolve",
    } <= {r.path for r in routes}
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if compliance_gate not in {d.call for d in route.dependant.dependencies}
    ]
    assert not offenders, f"Compliance routes without the MFA compliance gate: {offenders}"
