#!/bin/bash
# /check: run the HustlerCore Swift package tests. Fast; no simulator.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
CORE_DIR="${HUSTLERCORE_DIR:-ios-app/HustlerCore}"
SWIFT_JOBS="${SWIFT_JOBS:-2}"

cd "$REPO_DIR"
echo "[check] repo: $REPO_DIR"
echo "[check] package: $CORE_DIR"

if ! command -v swift >/dev/null 2>&1; then
  echo "[check] swift not found." >&2
  echo "[check] Install Xcode, then: sudo xcode-select -s /Applications/Xcode.app" >&2
  exit 127
fi
echo "[check] $(swift --version 2>&1 | head -n 1)"

# -j keeps SwiftPM inside the memory budget of an 8 GB M1.
exec swift test --package-path "$CORE_DIR" -j "$SWIFT_JOBS"
