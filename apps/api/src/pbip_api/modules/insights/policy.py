"""Object-level authorization for RM insights: which customers a staff member may see (ADR-0032).

An administrator sees every customer. A Relationship Manager sees only the customers assigned to
them; any other customer is reported exactly like one that does not exist (404), so an id cannot be
probed. The scope comes only from the authenticated principal, never from the request.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.insights import repository


@dataclass(frozen=True)
class RmScope:
    """Who is looking: ``sees_all`` for ADMIN, otherwise only ``user_id``'s portfolio."""

    user_id: UUID
    sees_all: bool


def scope_for(user_id: UUID, roles: Iterable[str]) -> RmScope:
    """ADMIN wins over RELATIONSHIP_MANAGER for a user holding both roles."""
    return RmScope(user_id=user_id, sees_all=RoleName.ADMIN.value in set(roles))


def visible_customer_ids(session: Session, scope: RmScope) -> set[UUID] | None:
    """The customers in scope, or None for "every customer"."""
    if scope.sees_all:
        return None
    return repository.assigned_customer_ids(session, scope.user_id)


def require_visible_customer(session: Session, scope: RmScope, customer_id: UUID) -> None:
    """404 when the customer does not exist or is not in the caller's portfolio (same answer)."""
    if repository.get_customer(session, customer_id) is None:
        raise NotFoundError("customer not found")
    if not scope.sees_all and not repository.is_assigned(session, customer_id, scope.user_id):
        raise NotFoundError("customer not found")
