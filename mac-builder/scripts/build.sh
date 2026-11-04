#!/bin/bash
# /build: xcodegen, then xcodebuild build on exactly one simulator, then shut it
# down. Run when the scheme has no test action (test.sh exits 78), so the app is
# still compiled: a diff that does not build is not a diff that works.
#
# Exit codes:
#   0   the app built
#   2   no ios-app/project.yml, or no available iPhone simulator
#   127 xcodegen, xcodebuild or xcrun is missing
#   *   xcodebuild's own failure code
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT="${XCODE_PROJECT:-ios-app/Hustler.xcodeproj}"
SCHEME="${XCODE_SCHEME:-Hustler}"
WANTED_SIMULATOR="${SIMULATOR_NAME:-}"
BUILD_JOBS="${XCODEBUILD_JOBS:-2}"

cd "$REPO_DIR"
echo "[build] repo: $REPO_DIR"

for tool in xcodegen xcodebuild xcrun; do
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

# A template path, not -t: BSD (macOS) mktemp takes a prefix there, while GNU
# mktemp wants the X's in a template and fails with "too few X's".
sim_log="$(mktemp "${TMPDIR:-/tmp}/macbuilder-sim.XXXXXX")"
build_log="$(mktemp "${TMPDIR:-/tmp}/macbuilder-build.XXXXXX")"
udid=""

cleanup() {
  if [ -n "$udid" ]; then
    echo "[build] shutting down simulator $udid"
    xcrun simctl shutdown "$udid" >/dev/null 2>&1 || true
  fi
  rm -f "$sim_log" "$build_log"
}
trap cleanup EXIT INT TERM

xcrun simctl list devices available > "$sim_log"
if [ -n "$WANTED_SIMULATOR" ]; then
  udid="$(grep -F "    $WANTED_SIMULATOR (" "$sim_log" | head -n 1 | sed -E 's/.*\(([0-9A-Fa-f-]{36})\).*/\1/' || true)"
fi
if [ -z "$udid" ]; then
  udid="$(grep -E '^[[:space:]]+iPhone .*\([0-9A-Fa-f-]{36}\)' "$sim_log" | head -n 1 | sed -E 's/.*\(([0-9A-Fa-f-]{36})\).*/\1/')"
  if [ -n "$udid" ] && [ -n "$WANTED_SIMULATOR" ]; then
    echo "[build] simulator '$WANTED_SIMULATOR' not found; using the first available iPhone"
  fi
fi
if [ -z "$udid" ]; then
  echo "[build] no available iPhone simulator." >&2
  echo "[build] Open Xcode once as this user, or: xcrun simctl create 'iPhone 16' com.apple.CoreSimulator.SimDeviceType.iPhone-16" >&2
  exit 2
fi
echo "[build] simulator: $udid"

set +e
xcodebuild build \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -destination "platform=iOS Simulator,id=$udid" \
  -derivedDataPath ios-app/.build/DerivedData \
  -jobs "$BUILD_JOBS" \
  CODE_SIGNING_ALLOWED=NO \
  2>&1 | tee "$build_log"
status=${PIPESTATUS[0]}
set -e

if [ "$status" -ne 0 ]; then
  echo "[build] xcodebuild build failed with exit $status" >&2
  exit "$status"
fi

echo "[build] BUILD SUCCEEDED on simulator $udid"
