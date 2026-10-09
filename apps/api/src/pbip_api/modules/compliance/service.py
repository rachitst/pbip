"""Tamper-evident audit trail.

Each row stores ``hash = SHA256(prev_hash || canonical(row))``. Rows are append-only (database
trigger) and the chain can be re-verified at any time; editing or deleting a row, even as a DB
superuser who disables the trigger, breaks every later hash. Writers serialise on a Postgres
advisory lock so the chain has no forks.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.modules.compliance.models import AuditLog

GENESIS_HASH = "0" * 64
_AUDIT_LOCK_KEY = 724_001
_VERIFY_BATCH = 500


def compute_hash(
    prev_hash: str,
    row_id: UUID,
    actor_user_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    created_at: datetime,
    details: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "prev": prev_hash,
            "id": str(row_id),
            "actor": str(actor_user_id) if actor_user_id else None,
            "action": action,
            "rt": resource_type,
            "rid": resource_id,
            "ts": created_at.astimezone(UTC).isoformat(),
            "details": details,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainStatus:
    valid: bool
    checked: int
    first_invalid_seq: int | None


class AuditService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        actor_user_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        *,
        ip: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Append an audit row in the CALLER'S transaction (rolls back with the business change)."""
        details = details or {}
        self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _AUDIT_LOCK_KEY})
        prev_hash = (
            self.session.execute(select(AuditLog.hash).order_by(AuditLog.seq.desc()).limit(1))
            .scalars()
            .first()
        ) or GENESIS_HASH
        row_id = uuid4()
        created_at = utcnow()
        row = AuditLog(
            id=row_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            ip=ip,
            details=details,
            created_at=created_at,
            prev_hash=prev_hash,
            hash=compute_hash(
                prev_hash,
                row_id,
                actor_user_id,
                action,
                resource_type,
                resource_id,
                created_at,
                details,
            ),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def verify_chain(self) -> ChainStatus:
        expected_prev = GENESIS_HASH
        checked = 0
        last_seq = 0
        while True:
            batch = (
                self.session.execute(
                    select(AuditLog)
                    .where(AuditLog.seq > last_seq)
                    .order_by(AuditLog.seq)
                    .limit(_VERIFY_BATCH)
                )
                .scalars()
                .all()
            )
            if not batch:
                return ChainStatus(True, checked, None)
            for row in batch:
                recomputed = compute_hash(
                    row.prev_hash,
                    row.id,
                    row.actor_user_id,
                    row.action,
                    row.resource_type,
                    row.resource_id,
                    row.created_at,
                    row.details,
                )
                if row.prev_hash != expected_prev or row.hash != recomputed:
                    return ChainStatus(False, checked, row.seq)
                expected_prev = row.hash
                checked += 1
                last_seq = row.seq

    def list_logs(self, limit: int, before_seq: int | None) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.seq.desc()).limit(limit)
        if before_seq is not None:
            stmt = stmt.where(AuditLog.seq < before_seq)
        return list(self.session.execute(stmt).scalars().all())
