#!/bin/bash
# /build: xcodegen, then an unsigned iOS Simulator build.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT="${XCODE_PROJECT:-ios-app/Hustler.xcodeproj}"
SCHEME="${XCODE_SCHEME:-Hustler}"
BUILD_JOBS="${XCODEBUILD_JOBS:-2}"

cd "$REPO_DIR"
echo "[build] repo: $REPO_DIR"

for tool in xcodegen xcodebuild; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[build] $tool not found." >&2
    echo "[build] Install Xcode and XcodeGen (brew install xcodegen)." >&2
    exit 127
  fi
done
echo "[build] $(xcodegen --version 2>&1 | head -n 1)"

if [ ! -f ios-app/project.yml ]; then
  echo "[build] ios-app/project.yml not found in $REPO_DIR" >&2
  exit 2
fi
(cd ios-app && xcodegen generate --spec project.yml)

exec xcodebuild build \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath ios-app/.build/DerivedData \
  -jobs "$BUILD_JOBS" \
  CODE_SIGNING_ALLOWED=NO
