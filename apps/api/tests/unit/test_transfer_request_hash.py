"""The idempotency request hash is persisted, so its canonical form is a stored format.

A retry inside the key's TTL can span a deploy: if the field names, the separators or the key
order changed, the replay would hash differently and be rejected as "a different request".
Pinning the exact canonical JSON makes any such change a deliberate, reviewed one (TASK-086).
"""

import hashlib
from uuid import UUID

import pytest
from pbip_api.modules.payments.service import PaymentsService, TransferCommand

pytestmark = pytest.mark.unit

USER = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT = UUID("22222222-2222-2222-2222-222222222222")
OTHER = UUID("33333333-3333-3333-3333-333333333333")


def _sha(canonical: str) -> str:
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_payee_transfer_hash_is_the_sha256_of_the_pinned_canonical_json() -> None:
    command = TransferCommand(ACCOUNT, 12_345, "INR", OTHER, None, "rent")
    canonical = (
        '{"amount":12345,"currency":"INR",'
        '"from":"22222222-2222-2222-2222-222222222222",'
        '"payee":"33333333-3333-3333-3333-333333333333",'
        '"reference":"rent","to":null,'
        '"user":"11111111-1111-1111-1111-111111111111"}'
    )
    assert PaymentsService._request_hash(USER, command) == _sha(canonical)


def test_own_account_transfer_hash_is_the_sha256_of_the_pinned_canonical_json() -> None:
    command = TransferCommand(ACCOUNT, 12_345, "INR", None, OTHER, None)
    canonical = (
        '{"amount":12345,"currency":"INR",'
        '"from":"22222222-2222-2222-2222-222222222222",'
        '"payee":null,"reference":null,'
        '"to":"33333333-3333-3333-3333-333333333333",'
        '"user":"11111111-1111-1111-1111-111111111111"}'
    )
    assert PaymentsService._request_hash(USER, command) == _sha(canonical)
