#!/usr/bin/env bash
# GitHub repository setup wrapper.
# Delegates to cross-platform scripts/setup_github.py.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/setup_github.py" "$@"
