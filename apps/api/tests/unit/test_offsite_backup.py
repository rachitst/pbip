"""The off-host backup copy through OCI's S3-compatible API (deploy guide section 15).

Only the parts that hold no network are tested here: reading the credentials file safely and building the
endpoint. The upload itself was proved on the staging host with a download, a byte comparison and a restore
drill of the downloaded copy (runbook section 4.2).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import scripts.offsite_backup as offsite

pytestmark = pytest.mark.unit

GOOD = (
    "# OCI Customer Secret Key for the pbip-backups bucket\n"
    "export OCI_NAMESPACE=axabc123xyz\n"
    'OCI_REGION="ap-mumbai-1"\n'
    "OCI_BUCKET=pbip-backups\n"
    "OCI_S3_ACCESS_KEY=test-access-id\n"
    "OCI_S3_SECRET_KEY='test-secret=with=equals'\n"  # pragma: allowlist secret
)


def _write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "oci.env"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_credentials_are_read_with_export_quotes_comments_and_equals_in_values(
    tmp_path: Path,
) -> None:
    creds = offsite.load_credentials(_write(tmp_path, GOOD))
    assert creds["OCI_NAMESPACE"] == "axabc123xyz"
    assert creds["OCI_REGION"] == "ap-mumbai-1"
    assert creds["OCI_S3_SECRET_KEY"] == "test-secret=with=equals"  # pragma: allowlist secret


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_a_credentials_file_others_can_read_is_refused_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(offsite.CredentialsError, match="mode 600") as err:
        offsite.load_credentials(_write(tmp_path, GOOD, mode=0o644))
    assert "test-secret" not in str(err.value)


def test_missing_keys_are_named_but_no_value_is_echoed(tmp_path: Path) -> None:
    text = GOOD.replace("OCI_BUCKET=pbip-backups\n", "")
    with pytest.raises(offsite.CredentialsError, match="OCI_BUCKET") as err:
        offsite.load_credentials(_write(tmp_path, text))
    assert "test-secret" not in str(err.value) and "test-access-id" not in str(err.value)


def test_a_namespace_that_could_redirect_the_endpoint_is_refused(tmp_path: Path) -> None:
    text = GOOD.replace("axabc123xyz", "evil.example.com/x")
    with pytest.raises(offsite.CredentialsError, match="OCI_NAMESPACE"):
        offsite.load_credentials(_write(tmp_path, text))


def test_endpoint_is_the_tenancy_namespace_on_the_regions_compat_host() -> None:
    assert (
        offsite.endpoint("axabc123xyz", "ap-mumbai-1")
        == "https://axabc123xyz.compat.objectstorage.ap-mumbai-1.oraclecloud.com"
    )


def test_cli_exits_2_with_a_message_when_the_credentials_file_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert offsite.main(["--credentials", str(tmp_path / "absent.env"), "ls"]) == 2
    assert "does not exist" in capsys.readouterr().err
