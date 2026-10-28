"""The operations runbook must only cite things that exist: files, scripts, make targets, API paths,
compose services and environment variables. A runbook that names a renamed script or endpoint fails
exactly when it is needed, so this keeps `docs/operations/runbook.md` honest as the code moves."""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
RUNBOOK = ROOT / "docs" / "operations" / "runbook.md"

_PATH_REF = re.compile(
    r"`((?:apps|docs|infra|scripts|\.github)/[A-Za-z0-9_./-]+\.(?:py|sh|md|yml|yaml|json|conf|alloy))`"
)
_MAKE_REF = re.compile(r"`make ([a-z-]+)")
_API_REF = re.compile(r"`(?:GET|POST|PUT|DELETE|PATCH) (/api/[A-Za-z0-9_/{}.-]+)`")
_ENV_TABLE_ROW = re.compile(r"^\| `([A-Z_]+)`(?:, `([A-Z_]+)`)?(?:, `([A-Z_]+)`)? \|", re.M)
_SERVICE_ROW = re.compile(r"^\| `([a-z]+)` \| ", re.M)
_SCRIPT_CALL = re.compile(r"python3? (scripts/[a-z_]+\.py)")
_MODULE_REF = re.compile(
    r"`((?:core|auth|compliance|fraud|payments|customers|ledger)/[a-z_]+\.py)`"
)
_API = ROOT / "apps" / "api" / "src" / "pbip_api"


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_cited_repository_files_exist() -> None:
    missing = sorted({p for p in _PATH_REF.findall(_text()) if not (ROOT / p).exists()})
    assert not missing, f"runbook cites missing files: {missing}"


def test_cited_module_files_exist() -> None:
    missing = sorted(
        {
            p
            for p in _MODULE_REF.findall(_text())
            if not ((_API / p).is_file() or (_API / "modules" / p).is_file())
        }
    )
    assert not missing, f"runbook cites missing module files: {missing}"


def test_invoked_scripts_exist() -> None:
    missing = sorted({p for p in _SCRIPT_CALL.findall(_text()) if not (ROOT / p).is_file()})
    assert not missing, f"runbook invokes missing scripts: {missing}"


def test_cited_make_targets_exist() -> None:
    targets = set(re.findall(r"^([a-z-]+):", (ROOT / "Makefile").read_text(encoding="utf-8"), re.M))
    missing = sorted(set(_MAKE_REF.findall(_text())) - targets)
    assert not missing, f"runbook cites missing make targets: {missing}"


def test_cited_api_paths_exist_in_the_openapi_contract() -> None:
    spec = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))

    def normalise(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path)

    known = {normalise(p) for p in spec["paths"]} | {"/api/health/live", "/api/health/ready"}
    cited = {normalise(p) for p in _API_REF.findall(_text())}
    assert cited, "expected the runbook to cite API paths"
    missing = sorted(cited - known)
    assert not missing, f"runbook cites API paths not in the contract: {missing}"


def test_environment_variables_in_the_table_are_real() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    prod_example = (ROOT / "infra" / "deploy" / ".env.prod.example").read_text(encoding="utf-8")
    compose = (ROOT / "infra" / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    config = (ROOT / "apps" / "api" / "src" / "pbip_api" / "core" / "config.py").read_text(
        encoding="utf-8"
    )
    cited = {name for row in _ENV_TABLE_ROW.findall(_text()) for name in row if name}
    assert "JWT_SECRET" in cited and "FIELD_ENCRYPTION_KEY" in cited
    unknown = sorted(
        name
        for name in cited
        if f"{name}=" not in env_example
        and f"{name}=" not in prod_example
        and f"${{{name}" not in compose
        and name.lower() not in config
    )
    assert not unknown, f"runbook env table names unknown variables: {unknown}"


def test_services_table_matches_the_production_compose_file() -> None:
    compose = (ROOT / "infra" / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    services_block = compose.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    services = set(re.findall(r"^  ([a-z]+):$", services_block, re.M))
    cited = set(_SERVICE_ROW.findall(_text()))
    assert cited == services, (
        f"runbook services {sorted(cited)} != compose services {sorted(services)}"
    )
