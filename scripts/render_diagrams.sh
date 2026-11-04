#!/usr/bin/env bash
# Render every Mermaid block in docs/diagrams/*.md to SVG and PNG (docs tooling only).
#
# Each diagram block is preceded by a marker comment that names its export files:
#   <!-- diagram: activity-login-mfa -->
#   ```mermaid
#   ...
#   ```
# and is written to docs/diagrams/svg/<name>.svg and docs/diagrams/png/<name>.png.
#
# Requirements: Node.js and npm. The pinned @mermaid-js/mermaid-cli is installed from
# docs/diagrams/package-lock.json (npm ci). Puppeteer does not download a browser
# (docs/diagrams/.puppeteerrc.cjs): it uses the Chromium Playwright installed for the E2E suite
# (`npx playwright install chromium` in apps/web), or the browser named by PBIP_CHROME_PATH.
#
# Usage: bash scripts/render_diagrams.sh [name ...]   (no names: render all)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIAGRAMS="$ROOT/docs/diagrams"
MMDC="$DIAGRAMS/node_modules/.bin/mmdc"

find_chrome() {
  if [[ -n "${PBIP_CHROME_PATH:-}" ]]; then
    echo "$PBIP_CHROME_PATH"
    return
  fi
  local cache="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
  [[ -d "$cache" ]] || cache="$HOME/Library/Caches/ms-playwright"
  local candidate
  for candidate in $(ls -d "$cache"/chromium-* 2>/dev/null | sort -V -r); do
    for exe in chrome-linux64/chrome chrome-linux/chrome \
      "chrome-mac/Chromium.app/Contents/MacOS/Chromium" \
      "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
      "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"; do
      if [[ -x "$candidate/$exe" ]]; then
        echo "$candidate/$exe"
        return
      fi
    done
  done
  echo "No Playwright Chromium found under $cache. Run 'npx playwright install chromium' in apps/web," >&2
  echo "or set PBIP_CHROME_PATH to a Chromium or Chrome executable." >&2
  exit 1
}

if [[ ! -x "$MMDC" ]]; then
  echo "==> Installing the pinned mermaid-cli (docs/diagrams/package-lock.json)"
  (cd "$DIAGRAMS" && PUPPETEER_SKIP_DOWNLOAD=1 npm ci --no-audit --no-fund)
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CHROME="$(find_chrome)"
printf '{"executablePath": "%s", "args": ["--no-sandbox", "--disable-gpu"]}\n' "$CHROME" >"$WORK/puppeteer.json"

# Split the Markdown files into one .mmd file per named block.
for md in "$DIAGRAMS"/*.md; do
  awk -v out="$WORK" '
    /^<!-- diagram: [a-z0-9-]+ -->$/ { name = $3; next }
    /^```mermaid$/ && name != "" { file = out "/" name ".mmd"; inblock = 1; next }
    /^```$/ && inblock { inblock = 0; name = ""; close(file); next }
    inblock { print > file }
  ' "$md"
done

mkdir -p "$DIAGRAMS/svg" "$DIAGRAMS/png"
count=0
for mmd in "$WORK"/*.mmd; do
  name="$(basename "$mmd" .mmd)"
  if [[ $# -gt 0 ]] && [[ ! " $* " == *" $name "* ]]; then
    continue
  fi
  echo "==> $name"
  "$MMDC" -q -p "$WORK/puppeteer.json" -c "$DIAGRAMS/mermaid-config.json" -b white \
    -i "$mmd" -o "$DIAGRAMS/svg/$name.svg"
  "$MMDC" -q -p "$WORK/puppeteer.json" -c "$DIAGRAMS/mermaid-config.json" -b white -s 2 \
    -i "$mmd" -o "$DIAGRAMS/png/$name.png"
  # Match the repository's pre-commit hooks (trailing-whitespace, end-of-file-fixer) so a fresh
  # render commits without the hooks rewriting it.
  sed -i.bak -e 's/[[:space:]]*$//' "$DIAGRAMS/svg/$name.svg" && rm -f "$DIAGRAMS/svg/$name.svg.bak"
  [[ -z "$(tail -c 1 "$DIAGRAMS/svg/$name.svg")" ]] || echo >>"$DIAGRAMS/svg/$name.svg"
  count=$((count + 1))
done
echo "Rendered $count diagram(s) into docs/diagrams/svg and docs/diagrams/png"
