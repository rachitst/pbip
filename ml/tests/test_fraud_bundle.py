"""TASK-084: the frozen fraud bundle is reproducible, and install refuses anything not pinned."""

import io
import json
import tarfile
from pathlib import Path

import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import bundle as b
from pbip_ml.fraud.inference import load
from pbip_ml.fraud.train import run

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data, out = tmp_path_factory.mktemp("data"), tmp_path_factory.mktemp("model")
    _export_fraud(GeneratorConfig(seed=42, customers=600, months=4), data)
    run(data, out)
    (out / b.PROVENANCE).write_text(json.dumps({"alias": "champion"}) + "\n", "utf-8")
    return out


def _version(directory: Path) -> str:
    return str(json.loads((directory / "hybrid_params.json").read_text("utf-8"))["model_version"])


def _lock_for(data: bytes, model_dir: Path, **overrides: str) -> b.Lock:
    fields = {
        "model_version": _version(model_dir),
        "bundle_name": "bundle.tar.gz",
        "bundle_sha256": b.sha256_bytes(data),
        "manifest_sha256": b.sha256_bytes((model_dir / b.MANIFEST).read_bytes()),
        "url": "https://example.invalid/bundle.tar.gz",
    }
    fields.update(overrides)
    return b.Lock(**fields)


def _tar_gz(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


@pytest.mark.unit
def test_packing_the_same_files_twice_gives_the_same_bytes(model_dir: Path) -> None:
    first, second = b.pack(model_dir), b.pack(model_dir)
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == list(b.BUNDLE_FILES)
    assert {(m.mtime, m.uid, m.gid, m.mode) for m in members} == {(0, 0, 0, 0o644)}


@pytest.mark.unit
def test_install_puts_exactly_the_packed_model_in_place(model_dir: Path, tmp_path: Path) -> None:
    data = b.pack(model_dir)
    out = tmp_path / "fraud"
    out.mkdir()
    (out / "stale.txt").write_text("old model", "utf-8")
    assert b.install(data, _lock_for(data, model_dir), out) == _version(model_dir)
    assert sorted(p.name for p in out.iterdir()) == list(b.BUNDLE_FILES)
    for name in b.BUNDLE_FILES:
        assert (out / name).read_bytes() == (model_dir / name).read_bytes()
    load(out)  # what the API does at startup


@pytest.mark.unit
def test_a_bundle_that_differs_from_the_lock_is_refused_and_nothing_changes(
    model_dir: Path, tmp_path: Path
) -> None:
    data = b.pack(model_dir)
    out = tmp_path / "fraud"
    out.mkdir()
    (out / "current.txt").write_text("served model", "utf-8")
    tampered = data[:-1] + bytes([data[-1] ^ 1])
    with pytest.raises(b.BundleError, match="SHA-256"):
        b.install(tampered, _lock_for(data, model_dir), out)
    assert [p.name for p in out.iterdir()] == ["current.txt"]


@pytest.mark.unit
@pytest.mark.parametrize("bad_name", ["../escape.txt", "/etc/evil", "extra.txt"])
def test_a_bundle_with_an_unknown_or_escaping_member_is_refused(
    model_dir: Path, tmp_path: Path, bad_name: str
) -> None:
    members = {n: (model_dir / n).read_bytes() for n in b.BUNDLE_FILES}
    members[bad_name] = b"x"
    data = _tar_gz(members)
    with pytest.raises(b.BundleError, match="unexpected member"):
        b.install(data, _lock_for(data, model_dir), tmp_path / "fraud")
    assert not (tmp_path / "fraud").exists()
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.unit
def test_a_bundle_missing_a_file_is_refused(model_dir: Path, tmp_path: Path) -> None:
    data = _tar_gz({n: (model_dir / n).read_bytes() for n in b.BUNDLE_FILES if n != b.REPORT})
    with pytest.raises(b.BundleError, match=r"missing report\.json"):
        b.install(data, _lock_for(data, model_dir), tmp_path / "fraud")


@pytest.mark.unit
def test_the_lock_must_name_the_same_model_version_and_manifest(
    model_dir: Path, tmp_path: Path
) -> None:
    data = b.pack(model_dir)
    with pytest.raises(b.BundleError, match="the lock pins hybrid-other-9"):
        b.install(data, _lock_for(data, model_dir, model_version="hybrid-other-9"), tmp_path / "a")
    with pytest.raises(b.BundleError, match=r"manifest\.json does not match"):
        b.install(data, _lock_for(data, model_dir, manifest_sha256="0" * 64), tmp_path / "b")


@pytest.mark.unit
def test_packing_refuses_a_directory_the_serving_loader_refuses(
    model_dir: Path, tmp_path: Path
) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    for name in b.BUNDLE_FILES:
        (broken / name).write_bytes((model_dir / name).read_bytes())
    (broken / "lightgbm.txt").write_bytes(b"not the trained booster")
    with pytest.raises(b.BundleError, match="serving loader refuses"):
        b.pack(broken)


@pytest.mark.unit
@pytest.mark.parametrize("source", ["http://example.invalid/b.tgz", "file:///etc/passwd"])
def test_only_https_or_a_local_path_can_be_fetched(source: str) -> None:
    with pytest.raises(b.BundleError, match="only https"):
        b.fetch(source)


@pytest.mark.unit
def test_the_committed_lock_pins_the_served_champion_on_the_v1_release() -> None:
    lock = b.Lock.read(REPO / "ml" / "models" / "fraud-champion.lock.json")
    assert lock.model_version == "hybrid-lgbm-iforest-3"
    assert lock.url == (
        f"https://github.com/rachitst/pbip/releases/download/v1.0.0/{lock.bundle_name}"
    )
    assert lock.model_version in lock.bundle_name
    assert len(lock.bundle_sha256) == len(lock.manifest_sha256) == 64
