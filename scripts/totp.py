"""Print the current TOTP code for a seeded demo staff user (reads .dev-secrets.txt)."""

import sys
import time
from pathlib import Path

import pyotp


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: uv run python scripts/totp.py analyst@demo.pbip\n")
        return 2
    for line in Path(".dev-secrets.txt").read_text(encoding="utf-8").splitlines():
        email, secret = line.split()
        if email == sys.argv[1]:
            code = pyotp.TOTP(secret).now()
            remaining = 30 - int(time.time()) % 30
            sys.stdout.write(f"{code}  (valid for ~{remaining}s)\n")
            return 0
    sys.stderr.write("unknown user (was the DB seeded on this machine?)\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
