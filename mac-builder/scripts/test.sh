#!/bin/bash
# /test: xcodegen, then xcodebuild test on exactly one simulator, then shut it down.
#
# Exit codes:
#   0   tests passed
#   78  the scheme has no test action (this repository currently has none);
#       reported as "unavailable", not as a failed diff
#   *   xcodebuild's own failure code
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT="${XCODE_PROJECT:-ios-app/Hustler.xcodeproj}"
SCHEME="${XCODE_SCHEME:-Hustler}"
WANTED_SIMULATOR="${SIMULATOR_NAME:-}"
BUILD_JOBS="${XCODEBUILD_JOBS:-2}"

cd "$REPO_DIR"
echo "[test] repo: $REPO_DIR"

for tool in xcodegen xcodebuild xcrun; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[test] $tool not found." >&2
    echo "[test] Install Xcode and XcodeGen (brew install xcodegen)." >&2
    exit 127
  fi
done
echo "[test] $(xcodegen --version 2>&1 | head -n 1)"

if [ ! -f ios-app/project.yml ]; then
  echo "[test] ios-app/project.yml not found in $REPO_DIR" >&2
  exit 2
fi
(cd ios-app && xcodegen generate --spec project.yml)

sim_log="$(mktemp -t macbuilder-sim)"
build_log="$(mktemp -t macbuilder-test)"
udid=""

cleanup() {
  if [ -n "$udid" ]; then
    echo "[test] shutting down simulator $udid"
    xcrun simctl shutdown "$udid" >/dev/null 2>&1 || true
  fi
  rm -f "$sim_log" "$build_log"
}
trap cleanup EXIT INT TERM

xcrun simctl list devices available > "$sim_log"
if [ -n "$WANTED_SIMULATOR" ]; then
  udid="$(grep -F "    $WANTED_SIMULATOR (" "$sim_log" | head -n 1 | sed -E 's/.*\(([0-9A-Fa-f-]{36})\).*/\1/')"
fi
if [ -z "$udid" ]; then
  udid="$(grep -E '^[[:space:]]+iPhone .*\([0-9A-Fa-f-]{36}\)' "$sim_log" | head -n 1 | sed -E 's/.*\(([0-9A-Fa-f-]{36})\).*/\1/')"
  if [ -n "$udid" ] && [ -n "$WANTED_SIMULATOR" ]; then
    echo "[test] simulator '$WANTED_SIMULATOR' not found; using the first available iPhone"
  fi
fi
if [ -z "$udid" ]; then
  echo "[test] no available iPhone simulator." >&2
  echo "[test] Open Xcode once as this user, or: xcrun simctl create 'iPhone 16' com.apple.CoreSimulator.SimDeviceType.iPhone-16" >&2
  exit 2
fi
echo "[test] simulator: $udid"

set +e
xcodebuild test \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -destination "platform=iOS Simulator,id=$udid" \
  -derivedDataPath ios-app/.build/DerivedData \
  -jobs "$BUILD_JOBS" \
  -parallel-testing-enabled NO \
  CODE_SIGNING_ALLOWED=NO \
  2>&1 | tee "$build_log"
status=${PIPESTATUS[0]}
set -e

if [ "$status" -ne 0 ]; then
  if grep -q "not configured for the test action" "$build_log"; then
    echo "[test] scheme '$SCHEME' has no test action." >&2
    echo "[test] The Swift tests live in HustlerCore and run via /check." >&2
    echo "[test] exit 78 = test stage unavailable (not a diff failure)." >&2
    exit 78
  fi
  echo "[test] xcodebuild test failed with exit $status" >&2
  exit "$status"
fi

echo "[test] tests passed on simulator $udid"
