"""RM-to-customer assignment (TASK-056 follow-up #79, ADR-0032).

- an RM sees only their assigned customers (list, detail, outreach log); others are 404, not 403
- ADMIN sees everyone, including unassigned customers; the list says which scope it used
- only ADMIN with MFA assigns; a reassignment moves visibility and keeps the history
- every change is audited (who, customer, RM); idempotent PUT/DELETE write nothing twice
- concurrent assignments of one customer leave exactly one active assignment (real threads)
- the migration goes down and up again
"""

import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName, User, UserStatus
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.insights import service
from pbip_test_support import PASSWORD, Actor
from sqlalchemy import Engine, inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
LIST = "/api/v1/insights/customers"
RMS = "/api/v1/insights/relationship-managers"
NOTE = {"channel": "CALL", "outcome": "FOLLOW_UP", "note": "Introduced myself."}


def _assignment(customer: Actor) -> str:
    return f"{LIST}/{customer.customer_id}/assignment"


def _ids(body: dict[str, Any]) -> set[str]:
    return {item["customer_id"] for item in body["items"]}


def _assign(client: TestClient, admin: Actor, customer: Actor, rm: Actor) -> Any:
    return client.put(
        _assignment(customer), headers=admin.headers, json={"rm_user_id": str(rm.user_id)}
    )


def _audit(db: Session, action: str) -> list[Any]:
    return list(
        db.execute(
            text(
                "SELECT actor_user_id, resource_type, resource_id, details FROM audit_logs "
                "WHERE action = :a ORDER BY seq"
            ),
            {"a": action},
        ).all()
    )


def _sees(client: TestClient, rm: Actor, customer: Actor) -> bool:
    """True when every customer-level RM route answers; False when every one is 404."""
    base = f"{LIST}/{customer.customer_id}"
    codes = {
        client.get(base, headers=rm.headers).status_code,
        client.get(f"{base}/interactions", headers=rm.headers).status_code,
    }
    in_list = str(customer.customer_id) in _ids(client.get(LIST, headers=rm.headers).json())
    if codes == {200} and in_list:
        return True
    assert codes == {404} and not in_list, (codes, in_list)
    return False


def test_an_rm_sees_only_assigned_customers_and_admin_sees_everyone(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    mine, theirs, nobodys = (make_customer(funds=10_000_00) for _ in range(3))
    rm, other_rm = (
        make_staff(RoleName.RELATIONSHIP_MANAGER),
        make_staff(RoleName.RELATIONSHIP_MANAGER),
    )
    admin = make_staff(RoleName.ADMIN)
    assert _assign(client, admin, mine, rm).status_code == 200
    assert _assign(client, admin, theirs, other_rm).status_code == 200

    body = client.get(LIST, headers=rm.headers).json()
    assert (_ids(body), body["total"], body["scope"]) == ({str(mine.customer_id)}, 1, "ASSIGNED")
    assert _sees(client, rm, mine)
    assert not _sees(client, rm, theirs)  # another RM's customer: 404, like an unknown id
    assert not _sees(client, rm, nobodys)  # unassigned customers are ADMIN-only
    unknown = client.get(f"{LIST}/{uuid.uuid4()}", headers=rm.headers)
    hidden = client.get(f"{LIST}/{theirs.customer_id}", headers=rm.headers)
    assert unknown.status_code == hidden.status_code == 404
    same = ("type", "title", "status", "code")
    assert {k: unknown.json().get(k) for k in same} == {k: hidden.json().get(k) for k in same}
    post = client.post(f"{LIST}/{theirs.customer_id}/interactions", headers=rm.headers, json=NOTE)
    assert post.status_code == 404
    # filters and paging stay inside the portfolio
    assert client.get(f"{LIST}?segment=UNSEGMENTED", headers=rm.headers).json()["total"] == 1
    assert client.get(f"{LIST}?offset=1", headers=rm.headers).json()["items"] == []

    everyone = client.get(LIST, headers=admin.headers).json()
    assert everyone["scope"] == "ALL" and everyone["total"] == 3
    assert _ids(everyone) == {str(c.customer_id) for c in (mine, theirs, nobodys)}
    detail = client.get(f"{LIST}/{nobodys.customer_id}", headers=admin.headers).json()
    assert detail["assigned_rm"] is None
    detail = client.get(f"{LIST}/{mine.customer_id}", headers=admin.headers).json()
    assert (detail["assigned_rm"]["rm_user_id"], detail["assigned_rm"]["rm_email"]) == (
        str(rm.user_id),
        rm.email,
    )


def test_a_user_with_both_roles_is_treated_as_admin(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    customer = make_customer()
    admin_rm = make_staff(RoleName.ADMIN)
    db.execute(
        text("INSERT INTO user_roles (user_id, role_name) VALUES (:u, 'RELATIONSHIP_MANAGER')"),
        {"u": admin_rm.user_id},
    )
    db.commit()
    body = client.get(LIST, headers=admin_rm.headers).json()
    assert body["scope"] == "ALL" and _ids(body) == {str(customer.customer_id)}


def test_reassignment_moves_visibility_and_keeps_the_history(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer(funds=1_000_00)
    first, second = (
        make_staff(RoleName.RELATIONSHIP_MANAGER),
        make_staff(RoleName.RELATIONSHIP_MANAGER),
    )
    admin = make_staff(RoleName.ADMIN)

    assert _assign(client, admin, alice, first).status_code == 200
    assert (
        client.post(
            f"{LIST}/{alice.customer_id}/interactions", headers=first.headers, json=NOTE
        ).status_code
        == 201
    )
    assert _sees(client, first, alice) and not _sees(client, second, alice)

    moved = _assign(client, admin, alice, second)
    assert moved.status_code == 200
    assert moved.json()["assigned_rm"]["rm_user_id"] == str(second.user_id)
    assert not _sees(client, first, alice) and _sees(client, second, alice)
    notes = client.get(f"{LIST}/{alice.customer_id}/interactions", headers=second.headers).json()
    assert [n["logged_by"] for n in notes["items"]] == [
        first.email
    ]  # the log stays with the customer

    assert client.delete(_assignment(alice), headers=admin.headers).status_code == 204
    assert not _sees(client, second, alice)
    assert client.get(f"{LIST}/{alice.customer_id}", headers=admin.headers).status_code == 200

    rows = db.execute(
        text(
            "SELECT rm_user_id, assigned_by_user_id, unassigned_at, unassigned_by_user_id "
            "FROM rm_assignments WHERE customer_id = :c ORDER BY assigned_at, created_at"
        ),
        {"c": alice.customer_id},
    ).all()
    assert [(r.rm_user_id, r.assigned_by_user_id, r.unassigned_by_user_id) for r in rows] == [
        (first.user_id, admin.user_id, admin.user_id),
        (second.user_id, admin.user_id, admin.user_id),
    ]
    assert all(r.unassigned_at is not None for r in rows)  # history kept, nothing deleted


def test_assignment_changes_are_audited_once(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    first, second = (
        make_staff(RoleName.RELATIONSHIP_MANAGER),
        make_staff(RoleName.RELATIONSHIP_MANAGER),
    )
    admin = make_staff(RoleName.ADMIN)

    assert _assign(client, admin, alice, first).status_code == 200
    again = _assign(client, admin, alice, first)  # same RM: idempotent, no second row or audit
    assert again.status_code == 200 and again.json()["assigned_rm"]["rm_email"] == first.email
    assert _assign(client, admin, alice, second).status_code == 200
    assert client.delete(_assignment(alice), headers=admin.headers).status_code == 204
    assert client.delete(_assignment(alice), headers=admin.headers).status_code == 204  # retry

    cid = str(alice.customer_id)
    assigned = _audit(db, "RM_ASSIGNED")
    assert [(a.actor_user_id, a.resource_type, a.resource_id) for a in assigned] == [
        (admin.user_id, "customer", cid),
        (admin.user_id, "customer", cid),
    ]
    assert [a.details for a in assigned] == [
        {"rm_user_id": str(first.user_id), "previous_rm_user_id": None},
        {"rm_user_id": str(second.user_id), "previous_rm_user_id": str(first.user_id)},
    ]
    unassigned = _audit(db, "RM_UNASSIGNED")
    assert [(a.actor_user_id, a.resource_id, a.details) for a in unassigned] == [
        (admin.user_id, cid, {"rm_user_id": str(second.user_id)})
    ]
    assert db.execute(text("SELECT count(*) FROM rm_assignments")).scalar_one() == 2


def test_only_admin_with_mfa_can_assign_or_list_rms(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    callers: list[tuple[dict[str, str] | None, int]] = [
        (None, 401),
        (alice.headers, 403),
        (rm.headers, 403),  # an RM cannot assign customers to themselves
        (make_staff(RoleName.FRAUD_ANALYST).headers, 403),
        (make_staff(RoleName.COMPLIANCE_OFFICER).headers, 403),
        (make_staff(RoleName.ADMIN, mfa=False).headers, 403),
    ]
    body = {"rm_user_id": str(rm.user_id)}
    for headers, expected in callers:
        assert client.put(_assignment(alice), headers=headers, json=body).status_code == expected
        assert client.delete(_assignment(alice), headers=headers).status_code == expected
        assert client.get(RMS, headers=headers).status_code == expected
    assert db.execute(text("SELECT count(*) FROM rm_assignments")).scalar_one() == 0

    admin = make_staff(RoleName.ADMIN)
    listed = client.get(RMS, headers=admin.headers).json()["items"]
    assert listed == [{"user_id": str(rm.user_id), "email": rm.email}]  # RMs only, no other staff


def test_invalid_assignments_are_rejected_and_nothing_is_written(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    admin = make_staff(RoleName.ADMIN)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    disabled_rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    db.execute(
        update(User).where(User.id == disabled_rm.user_id).values(status=UserStatus.DISABLED)
    )
    db.commit()
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)

    def put(path: str, body: dict[str, Any]) -> int:
        return client.put(path, headers=admin.headers, json=body).status_code

    ghost = f"{LIST}/{uuid.uuid4()}/assignment"
    assert put(ghost, {"rm_user_id": str(rm.user_id)}) == 404
    assert client.delete(ghost, headers=admin.headers).status_code == 404
    for target in (
        analyst.user_id,
        admin.user_id,
        disabled_rm.user_id,
        alice.user_id,
        uuid.uuid4(),
    ):
        assert put(_assignment(alice), {"rm_user_id": str(target)}) == 422, target
    for body in ({}, {"rm_user_id": "nope"}, {"rm_user_id": str(rm.user_id), "customer_id": "x"}):
        assert put(_assignment(alice), body) == 422, body
    assert put(f"{LIST}/not-a-uuid/assignment", {"rm_user_id": str(rm.user_id)}) == 422
    assert db.execute(text("SELECT count(*) FROM rm_assignments")).scalar_one() == 0
    assert (
        db.execute(
            text("SELECT count(*) FROM audit_logs WHERE action LIKE 'RM_%ASSIGNED'")
        ).scalar_one()
        == 0
    )


def _staff(factory: Callable[[Session], AuthService], engine: Engine, role: RoleName) -> uuid.UUID:
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        email = f"{role.value.lower()}-{uuid.uuid4().hex[:8]}@bank.example"
        return factory(session).create_user(email, PASSWORD, "Staff", [role], RequestMeta()).id


def test_concurrent_assignments_keep_exactly_one_active(
    make_customer: Callable[..., Actor],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """Six administrators assign one customer to six different RMs at the same moment."""
    customer_id = make_customer().customer_id
    assert customer_id is not None
    admin = _staff(auth_service_factory, migrated_engine, RoleName.ADMIN)
    rms = [
        _staff(auth_service_factory, migrated_engine, RoleName.RELATIONSHIP_MANAGER)
        for _ in range(6)
    ]
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    barrier = threading.Barrier(len(rms))
    errors: list[BaseException] = []

    def assign(rm_id: uuid.UUID) -> None:
        try:
            with factory() as session:
                barrier.wait()
                service.assign_rm(session, customer_id, rm_id, admin)
        except BaseException as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=assign, args=(rm_id,)) for rm_id in rms]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    active = (
        db.execute(
            text(
                "SELECT rm_user_id FROM rm_assignments WHERE customer_id = :c AND unassigned_at IS NULL"
            ),
            {"c": customer_id},
        )
        .scalars()
        .all()
    )
    assert len(active) == 1
    assert db.execute(text("SELECT count(*) FROM rm_assignments")).scalar_one() == len(rms)
    # The audit trail is one consistent chain: each change names the RM it replaced.
    details = [a.details for a in _audit(db, "RM_ASSIGNED")]
    assert [d["previous_rm_user_id"] for d in details[1:]] == [
        d["rm_user_id"] for d in details[:-1]
    ]
    assert details[0]["previous_rm_user_id"] is None
    assert details[-1]["rm_user_id"] == str(active[0])


def test_the_database_refuses_a_second_active_assignment(
    make_customer: Callable[..., Actor],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """The partial unique index is the safety net behind the service's lock."""
    customer_id = make_customer().customer_id
    admin = _staff(auth_service_factory, migrated_engine, RoleName.ADMIN)
    rm = _staff(auth_service_factory, migrated_engine, RoleName.RELATIONSHIP_MANAGER)
    insert = text(
        "INSERT INTO rm_assignments (customer_id, rm_user_id, assigned_by_user_id, assigned_at) "
        "VALUES (:c, :r, :a, now())"
    )
    db.execute(insert, {"c": customer_id, "r": rm, "a": admin})
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(insert, {"c": customer_id, "r": rm, "a": admin})
    db.rollback()
    with pytest.raises(IntegrityError):  # closed without saying who closed it
        db.execute(text("UPDATE rm_assignments SET unassigned_at = now()"))
    db.rollback()


def test_migration_goes_down_and_up(
    migrated_engine: Engine, test_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", test_database_url)
    config = Config(str(ALEMBIC_INI))
    scripts = ScriptDirectory.from_config(config)
    # Found by file name, so the test survives a renumbering at merge time.
    ours = next(r for r in scripts.walk_revisions() if r.path.endswith("_rm_assignments.py"))
    assert isinstance(ours.down_revision, str)
    try:
        command.downgrade(config, ours.down_revision)
        assert "rm_assignments" not in inspect(migrated_engine).get_table_names()
    finally:
        command.upgrade(config, "head")
    indexes = {i["name"]: i for i in inspect(migrated_engine).get_indexes("rm_assignments")}
    active = indexes["uq_rm_assignments_active_customer_id"]
    assert active["unique"] and active["column_names"] == ["customer_id"]
    assert "unassigned_at IS NULL" in str(active["dialect_options"]["postgresql_where"])
    with migrated_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version"))
        assert version.scalar_one() == scripts.get_current_head()
