"""Export (or verify) the committed OpenAPI contract at docs/api/openapi.json.

uv run python scripts/export_openapi.py          # regenerate
uv run python scripts/export_openapi.py --check  # fail if the committed file is stale (CI)
"""

import argparse
import json
import sys
from pathlib import Path

from pbip_api.core.config import Settings
from pbip_api.main import create_app

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json"


def render() -> str:
    app = create_app(Settings(app_env="local"))
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    fresh = render()
    if args.check:
        current = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
        if current != fresh:
            sys.stderr.write(
                "docs/api/openapi.json is out of date. Run: make openapi (and commit the result)\n"
            )
            return 1
        return 0
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(fresh, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
