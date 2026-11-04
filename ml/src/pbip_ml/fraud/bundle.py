"""Frozen, checksummed bundle of the served fraud model (TASK-084).

The promoted champion lives in one laptop's MLflow registry, and ``models/`` is git-ignored, so
before this module a new machine got the served model only by re-training it or by an operator
copying ``models/fraud`` by hand. ``pack`` turns an exported champion directory into a
byte-for-byte reproducible ``.tar.gz``; it is attached to a GitHub release, and
``ml/models/fraud-champion.lock.json`` pins its SHA-256 in the repository. ``install`` downloads
(or reads) the bundle, refuses it unless its SHA-256 equals the pinned one, extracts only the six
known file names, checks that the serving loader accepts the files and that the model version
and manifest match the lock, and then swaps the directory in by renames.

Trust: the pinned SHA-256 comes from a reviewed commit, so a changed release asset is refused.
Extraction never follows member paths, links or directories (no path traversal).
"""

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pbip_ml.fraud.inference import ARTIFACTS, MANIFEST, ModelLoadError, load

REPORT = "report.json"
PROVENANCE = "champion.json"
BUNDLE_FILES = tuple(sorted((*ARTIFACTS, MANIFEST, REPORT, PROVENANCE)))
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 60


class BundleError(Exception):
    """The bundle is missing, altered, malformed or does not match the lock."""


@dataclass(frozen=True)
class Lock:
    """What a reviewed commit pins about the served bundle."""

    model_version: str
    bundle_name: str
    bundle_sha256: str
    manifest_sha256: str
    url: str

    @classmethod
    def read(cls, path: Path) -> "Lock":
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            return cls(
                model_version=str(raw["model_version"]),
                bundle_name=str(raw["bundle_name"]),
                bundle_sha256=str(raw["bundle_sha256"]),
                manifest_sha256=str(raw["manifest_sha256"]),
                url=str(raw["url"]),
            )
        except KeyError as exc:
            raise BundleError(f"lock file lacks {exc.args[0]}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack(model_dir: Path) -> bytes:
    """The bundle bytes for ``model_dir``: same files in, same bytes out, on any machine.

    Members are sorted, with mtime 0, owner 0:0 and mode 0644; the gzip header carries no name
    or time. The directory must load with the serving loader first, so a broken export is never
    packed.
    """
    _check_loads(model_dir)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in BUNDLE_FILES:
            data = (model_dir / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def _check_loads(directory: Path) -> str:
    """Return the model version if the serving loader accepts ``directory``."""
    missing = [n for n in BUNDLE_FILES if not (directory / n).is_file()]
    if missing:
        raise BundleError(f"missing {', '.join(missing)}")
    try:
        load(directory)
    except ModelLoadError as exc:
        raise BundleError(f"the serving loader refuses the files: {exc}") from exc
    params = json.loads((directory / "hybrid_params.json").read_text(encoding="utf-8"))
    return str(params["model_version"])


def fetch(source: str) -> bytes:
    """Read the bundle from a local path or an https URL (never another scheme)."""
    if source.startswith("https://"):
        # The scheme is checked on the line above, so urlopen only ever sees https.
        request = urllib.request.Request(source, headers={"User-Agent": "pbip-model-install"})  # noqa: S310  # nosec B310
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310  # nosec B310
            data: bytes = resp.read(_MAX_BUNDLE_BYTES + 1)
    elif "://" in source:
        raise BundleError("only https:// URLs or local paths are accepted")
    else:
        data = Path(source).read_bytes()
    if len(data) > _MAX_BUNDLE_BYTES:
        raise BundleError("bundle is larger than 64 MiB")
    return data


def _extract(data: bytes, dst: Path) -> None:
    """Write exactly the known regular files; any other member refuses the whole bundle."""
    dst.mkdir(parents=True)
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name not in BUNDLE_FILES or not member.isfile() or member.name in seen:
                    raise BundleError(f"unexpected member {member.name!r}")
                handle = tar.extractfile(member)
                if handle is None:
                    raise BundleError(f"unreadable member {member.name!r}")
                (dst / member.name).write_bytes(handle.read())
                seen.add(member.name)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BundleError(f"not a valid bundle: {type(exc).__name__}") from exc
    if seen != set(BUNDLE_FILES):
        raise BundleError(f"missing {', '.join(sorted(set(BUNDLE_FILES) - seen))}")


def install(data: bytes, lock: Lock, out_dir: Path) -> str:
    """Verify ``data`` against ``lock`` and swap it into ``out_dir``; return the model version."""
    if sha256_bytes(data) != lock.bundle_sha256:
        raise BundleError("bundle SHA-256 does not match the lock file")
    out_dir = out_dir.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir.parent, prefix=f".{out_dir.name}-") as tmp:
        staging = Path(tmp) / "new"
        _extract(data, staging)
        version = _check_loads(staging)
        if version != lock.model_version:
            raise BundleError(f"bundle holds {version}, the lock pins {lock.model_version}")
        if sha256_bytes((staging / MANIFEST).read_bytes()) != lock.manifest_sha256:
            raise BundleError("manifest.json does not match the lock file")
        parking = Path(tmp) / "previous"
        had_old = out_dir.exists()
        if had_old:
            out_dir.rename(parking)
        try:
            staging.rename(out_dir)
        except OSError:
            if had_old:
                parking.rename(out_dir)
            raise
    return version


# --------------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pack", help="write a reproducible bundle of an exported champion")
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True, help="the .tar.gz to write")
    i = sub.add_parser("install", help="verify the pinned bundle and install it")
    i.add_argument("--lock", type=Path, required=True)
    i.add_argument("--out", type=Path, required=True, help="the API's FRAUD_MODEL_DIR")
    i.add_argument("--bundle", default=None, help="local path or https URL (default: lock url)")
    return parser


def _run(args: argparse.Namespace) -> dict[str, str]:
    if args.command == "pack":
        data = pack(args.model_dir)
        args.out.write_bytes(data)
        manifest = sha256_bytes((args.model_dir / MANIFEST).read_bytes())
        return {
            "bundle": str(args.out),
            "bundle_sha256": sha256_bytes(data),
            "manifest_sha256": manifest,
            "model_version": _check_loads(args.model_dir),
        }
    lock = Lock.read(args.lock)
    version = install(fetch(args.bundle or lock.url), lock, args.out)
    return {
        "installed": str(args.out),
        "model_version": version,
        "bundle_sha256": lock.bundle_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0: packed or installed. 2: refused (nothing was changed)."""
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except (BundleError, OSError, ValueError) as exc:
        sys.stderr.write(f"model bundle refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
