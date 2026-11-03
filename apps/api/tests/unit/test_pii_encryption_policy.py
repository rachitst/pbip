"""The schema still matches the customer PII encryption policy (ADR-0042, issue #145).

ADR-0042 keeps email, full name and date of birth as plain columns and relies on field-level
encryption only for ``*_enc`` columns. These tests fail when the schema drifts from that record:
a new encrypted column that key rotation would skip (it becomes unreadable once the old key is
removed, runbook section 3.2), a plain-text phone column next to ``phone_enc``, or a change to the
email lookup the ADR's trade-off rests on. Any such change needs the ADR revisited, not this test.
"""

import pbip_api.modules  # noqa: F401  (registers every model on Base.metadata)
import pytest
from pbip_api.core.db import Base
from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from sqlalchemy import Date, String, Table, Text

from scripts import rotate_field_key

pytestmark = pytest.mark.unit


def _table(model: type[User] | type[Customer]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


def _encrypted_columns() -> set[str]:
    return {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.endswith("_enc")
    }


def test_every_field_encrypted_column_is_re_encrypted_by_key_rotation() -> None:
    rotated = {
        f"{_table(model).name}.{column}" for model, column in rotate_field_key.ENCRYPTED_COLUMNS
    }
    assert _encrypted_columns() == rotated == {"users.mfa_secret_enc", "customers.phone_enc"}


def test_encrypted_columns_are_unbounded_text_for_fernet_tokens() -> None:
    for name in _encrypted_columns():
        table, column = name.split(".")
        assert isinstance(Base.metadata.tables[table].columns[column].type, Text), name


def test_the_phone_number_exists_only_as_ciphertext() -> None:
    phone_columns = {c.name for c in _table(Customer).columns if "phone" in c.name}
    assert phone_columns == {"phone_enc"}


def test_plain_text_pii_columns_are_the_ones_the_adr_accepts() -> None:
    columns = _table(Customer).columns
    assert isinstance(columns["full_name"].type, String)
    assert isinstance(columns["date_of_birth"].type, Date)
    assert isinstance(_table(User).columns["email"].type, String)


def test_email_stays_a_case_insensitive_unique_login_key() -> None:
    """The reason email cannot be field-encrypted: Fernet output is randomised, so equal emails
    would get different ciphertexts and this unique index (and lookup by lower(email)) would break."""
    indexes = {str(index.name): index for index in _table(User).indexes}
    index = indexes["uq_users_email_lower"]
    assert index.unique
    assert [str(expression) for expression in index.expressions] == ["lower(email)"]
