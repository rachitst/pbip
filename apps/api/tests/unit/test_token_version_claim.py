"""ADR-0021: the `ver` claim round-trips, and tokens without it (issued before the change) read as 0."""

from datetime import timedelta
from uuid import uuid4

import jwt
import pytest
from pbip_api.core.config import Settings
from pbip_api.core.errors import UnauthorizedError
from pbip_api.core.security import decode_token, encode_token

pytestmark = pytest.mark.unit


def _settings() -> Settings:
    return Settings(jwt_secret="x" * 48)  # pragma: allowlist secret


def test_version_claim_round_trips() -> None:
    settings = _settings()
    token = encode_token(
        settings, subject=uuid4(), token_type="access", ttl=timedelta(minutes=1), version=7
    )
    assert decode_token(settings, token, "access").version == 7


def test_missing_version_claim_reads_as_zero() -> None:
    settings = _settings()
    token = encode_token(settings, subject=uuid4(), token_type="access", ttl=timedelta(minutes=1))
    payload = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
    payload.pop("ver")
    legacy = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    assert decode_token(settings, legacy, "access").version == 0


def test_tampered_version_breaks_the_signature() -> None:
    settings = _settings()
    token = encode_token(settings, subject=uuid4(), token_type="access", ttl=timedelta(minutes=1))
    header, payload, signature = token.split(".")
    forged = jwt.encode(
        {**jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"]), "ver": 99},
        "wrong-key-" + "y" * 40,
        algorithm="HS256",
    )
    with pytest.raises(UnauthorizedError, match="invalid or expired token"):
        decode_token(settings, f"{header}.{forged.split('.')[1]}.{signature}", "access")
