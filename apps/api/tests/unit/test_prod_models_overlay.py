"""ADR-0061: the opt-in overlay that serves the trained models on the staging host.

The overlay cannot be exercised against a real host in CI, so this pins what makes it safe: it is the only
place the production stack mounts models, the mounts are read-only (threat model T34), only the services that
load a model get the settings and files, the container paths match the local demo stack, and compose merges it
onto the production file without dropping the shared environment or the existing mounts.
"""

import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
DEPLOY = ROOT / "infra" / "deploy"
OVERLAY = DEPLOY / "docker-compose.prod.models.yml"
PROD = DEPLOY / "docker-compose.prod.yml"
LOCAL = ROOT / "docker-compose.yml"
MODEL_SETTINGS = ("FRAUD_SCORER", "FRAUD_MODEL_DIR", "RECOMMENDATION_MODEL_DIR")
FRAUD_MOUNT = "./models/fraud:/models/fraud:ro"
RECOMMENDATION_MOUNT = "./models/recommendations:/models/recommendations:ro"


def _services(path: Path) -> dict[str, dict[str, Any]]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    services: dict[str, dict[str, Any]] = loaded["services"]
    return services


def test_only_the_services_that_load_a_model_get_settings_and_files() -> None:
    """api scores transfers and ranks recommendations; the worker scores scheduled transfers
    (payments/scheduled_service.py) and ranks nothing; migrate, beat, web, db and redis load no model."""
    overlay = _services(OVERLAY)
    assert set(overlay) == {"api", "worker"}
    assert overlay["api"]["environment"] == {
        "FRAUD_SCORER": "hybrid",
        "FRAUD_MODEL_DIR": "/models/fraud",
        "FRAUD_MODEL_BUDGET_MS": "${FRAUD_MODEL_BUDGET_MS:-50}",
        "RECOMMENDATION_MODEL_DIR": "/models/recommendations",
    }
    assert overlay["api"]["volumes"] == [FRAUD_MOUNT, RECOMMENDATION_MOUNT]
    assert overlay["worker"]["environment"] == {
        "FRAUD_SCORER": "hybrid",
        "FRAUD_MODEL_DIR": "/models/fraud",
        "FRAUD_MODEL_BUDGET_MS": "${FRAUD_MODEL_BUDGET_MS:-50}",
    }
    assert overlay["worker"]["volumes"] == [FRAUD_MOUNT]


def test_every_model_mount_is_read_only_and_comes_from_the_host_models_directory() -> None:
    """T34: a container that could rewrite the files it loads could plant a pickle or weaken the model."""
    for name, service in _services(OVERLAY).items():
        for volume in service["volumes"]:
            source, target, mode = volume.split(":")
            assert mode == "ro", (name, volume)
            assert source.startswith("./models/") and target.startswith("/models/"), (name, volume)
            assert source.removeprefix(".") == target, (name, volume)


def test_the_overlay_only_adds_settings_and_mounts() -> None:
    """No image, command, port or dependency change: the release itself is the same with or without models."""
    for name, service in _services(OVERLAY).items():
        assert set(service) == {"environment", "volumes"}, name


def test_the_production_file_alone_serves_the_rules_and_mounts_no_model() -> None:
    """The default stays rules-only (FRAUD_SCORER code default `rules`, no model directory)."""
    text = PROD.read_text(encoding="utf-8")
    for setting in MODEL_SETTINGS:
        assert setting not in text, setting
    for name, service in _services(PROD).items():
        for volume in service.get("volumes", []):
            assert "/models" not in str(volume), (name, volume)


def test_container_paths_match_the_local_demo_stack() -> None:
    """The same directories as docker-compose.yml, so `make fraud-model` output copies over unchanged."""
    local = LOCAL.read_text(encoding="utf-8")
    assert "FRAUD_MODEL_DIR: /models/fraud" in local
    assert "RECOMMENDATION_MODEL_DIR: /models/recommendations" in local
    local_api_volumes = _services(LOCAL)["api"]["volumes"]
    assert FRAUD_MOUNT in local_api_volumes and RECOMMENDATION_MOUNT in local_api_volumes


def _compose_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    probe = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [docker, "compose", "version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


def _config(directory: Path, *files: str) -> dict[str, dict[str, Any]]:
    docker = shutil.which("docker")
    assert docker is not None
    args = [docker, "compose"]
    for name in files:
        args += ["-f", name]
    result = subprocess.run(  # noqa: S603 - our compose files in a temp directory, no shell
        [*args, "config", "--format", "json"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = yaml.safe_load(result.stdout)  # JSON is YAML
    services: dict[str, dict[str, Any]] = loaded["services"]
    return services


@pytest.mark.skipif(not _compose_available(), reason="needs the docker compose CLI (no daemon)")
def test_compose_merges_the_overlay_without_losing_the_shared_environment(tmp_path: Path) -> None:
    shutil.copy(PROD, tmp_path)
    shutil.copy(OVERLAY, tmp_path)
    # Generated per run: `config` only resolves variables, nothing connects anywhere.
    (tmp_path / ".env").write_text(
        "POSTGRES_USER=pbip\nPOSTGRES_DB=pbip\n"
        f"POSTGRES_PASSWORD={secrets.token_urlsafe(18)}\nJWT_SECRET={secrets.token_urlsafe(48)}\n"
        f"FIELD_ENCRYPTION_KEY={secrets.token_urlsafe(32)}\nCORS_ORIGINS=https://staging.example.test\n"
        "IMAGE_PREFIX=ghcr.io/example/pbip\nIMAGE_TAG=v0.1.0\n",
        encoding="utf-8",
    )
    base = _config(tmp_path, PROD.name)
    merged = _config(tmp_path, PROD.name, OVERLAY.name)

    assert set(merged) == set(base)
    added = {"api": set(MODEL_SETTINGS), "worker": {"FRAUD_SCORER", "FRAUD_MODEL_DIR"}}
    for name, service in merged.items():
        before = base[name].get("environment", {})
        after = service.get("environment", {})
        assert {k: after[k] for k in before} == before, name  # nothing of the anchor is dropped
        new = set(after) - set(before) - {"FRAUD_MODEL_BUDGET_MS"}
        assert new == added.get(name, set()), name
        targets_before = {v["target"] for v in base[name].get("volumes", [])}
        mounts = {v["target"]: v for v in service.get("volumes", [])}
        assert targets_before <= set(mounts), name  # geoip and the db CA stay mounted
        for target in set(mounts) - targets_before:
            assert target.startswith("/models/") and mounts[target]["read_only"] is True, name
    assert {v["target"] for v in merged["api"]["volumes"]} >= {
        "/models/fraud",
        "/models/recommendations",
    }
    assert merged["api"]["environment"]["FRAUD_MODEL_BUDGET_MS"] == "50"
