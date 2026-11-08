"""Contract of the deployment workflow (TASK-082, .github/workflows/deploy.yml).

The workflow cannot be executed in CI without a staging host, so this pins the properties that make it
safe: releases only from vX.Y.Z tags on main, least-privilege permissions, secrets never interpolated
into scripts, SSH host-key verification, a smoke test, and an automatic rollback step.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
_SHA_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")
_FIRST_PARTY = ("actions/", "github/")


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _steps(job: str) -> list[dict[str, Any]]:
    steps = _workflow()["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


def test_releases_come_only_from_version_tags_or_an_explicit_dispatch() -> None:
    on = _workflow()[True]  # PyYAML parses the bare key `on` as boolean True
    assert on["push"] == {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}
    assert "branches" not in on["push"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["tag"]["required"] is True
    assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False


def test_verify_job_rejects_tags_that_are_not_on_main() -> None:
    script = "\n".join(step.get("run", "") for step in _steps("verify"))
    assert "merge-base --is-ancestor" in script and "origin/main" in script
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in script
    assert _steps("verify")[0]["with"]["fetch-depth"] == 0


def test_permissions_are_least_privilege() -> None:
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["package"]["permissions"] == {"contents": "read", "packages": "write"}
    assert wf["jobs"]["deploy"]["permissions"] == {"contents": "read"}
    assert wf["concurrency"] == {"group": "deploy-staging", "cancel-in-progress": False}


def test_deploy_job_is_gated_and_uses_the_staging_environment() -> None:
    deploy = _workflow()["jobs"]["deploy"]
    assert deploy["environment"] == "staging"
    assert "vars.DEPLOY_ENABLED == 'true'" in deploy["if"]
    assert deploy["needs"] == ["verify", "package"]


def test_secrets_are_passed_through_env_never_interpolated_into_scripts() -> None:
    for job in ("verify", "package", "deploy"):
        for step in _steps(job):
            assert "${{ secrets." not in step.get("run", ""), f"{job}: secret in a run script"
    names = {step.get("name", "") for step in _steps("deploy")}
    assert any("names only" in name for name in names)
    check = next(s for s in _steps("deploy") if "names only" in s.get("name", ""))
    for secret in ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
        assert check["env"][secret] == f"${{{{ secrets.{secret} }}}}"
    assert check["env"]["STAGING_URL"] == "${{ vars.STAGING_URL }}"


def test_ssh_verifies_the_host_key_and_uses_batch_mode() -> None:
    ssh = next(s for s in _steps("deploy") if s.get("name", "").startswith("Configure SSH"))
    assert "StrictHostKeyChecking yes" in ssh["run"]
    assert "UserKnownHostsFile ~/.ssh/known_hosts" in ssh["run"]
    assert "BatchMode yes" in ssh["run"] and "IdentitiesOnly yes" in ssh["run"]
    assert "StrictHostKeyChecking=no" not in WORKFLOW.read_text(encoding="utf-8")


def test_deploy_runs_the_host_script_then_smoke_tests_then_rolls_back_on_failure() -> None:
    steps = _steps("deploy")
    names = [s.get("name", "") for s in steps]
    deploy_i = next(i for i, n in enumerate(names) if n.startswith("Deploy ("))
    smoke_i = next(i for i, n in enumerate(names) if n.startswith("Smoke test"))
    rollback_i = next(i for i, n in enumerate(names) if n.startswith("Roll back"))
    assert deploy_i < smoke_i < rollback_i
    assert "deploy.sh deploy" in steps[deploy_i]["run"]
    assert "scripts/smoke_test.sh" in steps[smoke_i]["run"]
    assert steps[rollback_i]["if"].startswith("failure()")
    assert "deploy.sh rollback" in steps[rollback_i]["run"]
    for step in steps:
        if step.get("name", "").startswith(
            ("Configure SSH", "Copy", "Deploy (", "Smoke", "Roll back")
        ):
            assert "env.DRY_RUN != 'true'" in step["if"], step.get("name")


def test_third_party_actions_are_pinned_to_a_commit_sha() -> None:
    for job, spec in _workflow()["jobs"].items():
        for step in spec["steps"]:
            uses = step.get("uses")
            if not uses or uses.startswith(_FIRST_PARTY):
                continue
            assert _SHA_PINNED.match(uses), f"{job}: {uses} is not pinned to a full commit SHA"


def test_host_script_is_shipped_and_syntactically_valid() -> None:
    script = ROOT / "infra" / "deploy" / "deploy.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "--wait" in text and "rollback)" in text
    copy = next(s for s in _steps("deploy") if s.get("name", "").startswith("Copy"))
    assert "infra/deploy/deploy.sh" in copy["run"] and "docker-compose.prod.yml" in copy["run"]


def _image_builds(workflow: Path, job: str) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = loaded["jobs"][job]["steps"]
    uses = [step.get("uses", "") for step in steps]
    qemu = next(i for i, u in enumerate(uses) if u.startswith("docker/setup-qemu-action@"))
    buildx = next(i for i, u in enumerate(uses) if u.startswith("docker/setup-buildx-action@"))
    assert qemu < buildx, f"{workflow.name}/{job}: QEMU must be registered before buildx starts"
    # The emulator image is pinned by digest (ADR-0006), not the action's default ":latest".
    assert "@sha256:" in steps[qemu]["with"]["image"]
    return [s for s in steps if s.get("uses", "").startswith("docker/build-push-action@")]


@pytest.mark.parametrize(
    ("workflow", "job"),
    [(WORKFLOW, "package"), (ROOT / ".github" / "workflows" / "ci.yml", "docker")],
)
def test_images_are_built_for_amd64_and_arm64(workflow: Path, job: str) -> None:
    """The staging host is an Arm64 VM (docs/operations/deploy-oracle-cloud-arm.md): a release must
    publish both platforms, and the PR build proves both still build before anything is tagged."""
    builds = _image_builds(workflow, job)
    assert {b["with"]["file"] for b in builds} == {"apps/api/Dockerfile", "apps/web/Dockerfile"}
    for build in builds:
        assert build["with"]["platforms"] == "linux/amd64,linux/arm64", build["with"]["file"]


def test_web_bundle_is_built_on_the_native_platform() -> None:
    """The static bundle is platform-independent: building it under emulation only costs time."""
    dockerfile = (ROOT / "apps" / "web" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM --platform=$BUILDPLATFORM node:" in dockerfile


def test_the_model_overlay_is_shipped_but_the_models_never_are() -> None:
    """ADR-0061: deploy.sh adds docker-compose.prod.models.yml when the host .env says ML_MODELS=enabled, so the
    file must be on the host for every release; the model files are operator-supplied, never copied by CI."""
    copy = next(s for s in _steps("deploy") if s.get("name", "").startswith("Copy"))
    assert "infra/deploy/docker-compose.prod.models.yml" in copy["run"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "models/fraud" not in text and "models/recommendations" not in text
