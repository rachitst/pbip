"""Off-host copy of PBIP backups to OCI Object Storage through its S3-compatible API.

The deploy guide (docs/operations/deploy-oracle-cloud-arm.md section 15) prefers an instance
principal with write-only rights. This is the alternative for a bucket reached with a Customer
Secret Key: the key pair sits in a mode-600 file on the host (never in the repository, never
printed), and boto3 is installed in a separate virtual environment on the host only, so it is not
a dependency of the project.

    offsite_backup.py --credentials ~/.pbip-oci-s3.env put FILE...
    offsite_backup.py --credentials ~/.pbip-oci-s3.env get NAME DESTDIR
    offsite_backup.py --credentials ~/.pbip-oci-s3.env ls

The credentials file holds OCI_NAMESPACE, OCI_REGION, OCI_BUCKET, OCI_S3_ACCESS_KEY and
OCI_S3_SECRET_KEY as KEY=value lines.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

REQUIRED = ("OCI_NAMESPACE", "OCI_REGION", "OCI_BUCKET", "OCI_S3_ACCESS_KEY", "OCI_S3_SECRET_KEY")
_NAME = re.compile(r"^[a-z0-9-]+$")


class CredentialsError(ValueError):
    """The credentials file is missing, readable by others, or incomplete. Never carries a value."""


def load_credentials(path: Path) -> dict[str, str]:
    """KEY=value lines; `export`, quotes and comments allowed. Refuses a file others can read."""
    if not path.is_file():
        raise CredentialsError(f"{path} does not exist")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CredentialsError(f"{path} must be mode 600 (it holds the secret key)")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    missing = [k for k in REQUIRED if not values.get(k)]
    if missing:
        raise CredentialsError(f"{path} lacks {', '.join(missing)}")
    for key in ("OCI_NAMESPACE", "OCI_REGION"):
        if not _NAME.match(values[key]):
            raise CredentialsError(f"{key} is not a valid OCI name")
    return values


def endpoint(namespace: str, region: str) -> str:
    """OCI's S3-compatible endpoint for a tenancy namespace and region."""
    return f"https://{namespace}.compat.objectstorage.{region}.oraclecloud.com"


def _client(creds: dict[str, str]) -> Any:  # boto3: untyped, host-only (module docstring)
    boto3 = importlib.import_module("boto3")
    config = importlib.import_module("botocore.config").Config(
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        region_name=creds["OCI_REGION"],
        endpoint_url=endpoint(creds["OCI_NAMESPACE"], creds["OCI_REGION"]),
        aws_access_key_id=creds["OCI_S3_ACCESS_KEY"],
        aws_secret_access_key=creds["OCI_S3_SECRET_KEY"],
        config=config,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--credentials", type=Path, default=Path.home() / ".pbip-oci-s3.env")
    sub = parser.add_subparsers(dest="command", required=True)
    put = sub.add_parser("put", help="upload files (object name = file name)")
    put.add_argument("files", nargs="+", type=Path)
    get = sub.add_parser("get", help="download one object")
    get.add_argument("name")
    get.add_argument("dest", type=Path)
    sub.add_parser("ls", help="list objects")
    args = parser.parse_args(argv)
    try:
        creds = load_credentials(args.credentials)
    except CredentialsError as exc:
        sys.stderr.write(f"offsite_backup: {exc}\n")
        return 2
    s3, bucket = _client(creds), creds["OCI_BUCKET"]
    if args.command == "put":
        for file in args.files:
            s3.upload_file(str(file), bucket, file.name)
            sys.stdout.write(f"uploaded {file.name} {file.stat().st_size}\n")
    elif args.command == "get":
        target = args.dest / args.name
        s3.download_file(bucket, args.name, str(target))
        sys.stdout.write(f"downloaded {target.name} {target.stat().st_size}\n")
    else:
        for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
            sys.stdout.write(f"{obj['Key']} {obj['Size']} {obj['LastModified'].isoformat()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
