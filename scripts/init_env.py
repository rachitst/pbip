"""Create .env from .env.example with freshly generated local secrets (cross-platform).

uv run python scripts/init_env.py [--force]

Standard library only: `setup` runs this before dependencies are installed (fresh clone).
"""

import base64
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fernet_key() -> str:
    """A Fernet key, in the same format as cryptography's Fernet.generate_key()."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def main() -> int:
    target, example = ROOT / ".env", ROOT / ".env.example"
    if target.exists() and "--force" not in sys.argv:
        sys.stdout.write(".env already exists (use --force to regenerate). Nothing changed.\n")
        return 0
    text = example.read_text(encoding="utf-8")
    replacements = {
        "JWT_SECRET": secrets.token_urlsafe(48),
        "FIELD_ENCRYPTION_KEY": fernet_key(),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(18),
        "GRAFANA_ADMIN_PASSWORD": secrets.token_urlsafe(18),
    }
    for key, value in replacements.items():
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
    # Keep DATABASE_URL/TEST_DATABASE_URL consistent with the generated Postgres password.
    password = replacements["POSTGRES_PASSWORD"]
    text = text.replace("pbip_local_password", password)
    target.write_text(text, encoding="utf-8")
    sys.stdout.write("Created .env with fresh local secrets. It is git-ignored: never commit it.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
