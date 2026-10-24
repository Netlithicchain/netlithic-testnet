#!/bin/sh
# Same as checksum.ps1, for Linux/macOS: desktop/checksum.sh dist/Netlithic.dmg
set -e
cd "$(dirname "$1")"
f=$(basename "$1")
if command -v sha256sum >/dev/null 2>&1; then sha256sum "$f" > "$f.sha256"; else shasum -a 256 "$f" > "$f.sha256"; fi
cat "$f.sha256"
