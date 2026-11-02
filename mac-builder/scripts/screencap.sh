#!/bin/bash
# Save the current Freebuff pane so a real screen can become a fixture.
#
#   scripts/screencap.sh                -> 01-step.txt  01-step-color.txt
#   scripts/screencap.sh sign-in        -> 02-sign-in.txt ...
#
# Nothing is typed into the session: capture-pane only, so a capture can never
# disturb a job that is waiting. Files go to mac-builder/logs/screencaps/
# (gitignored through logs/*) unless SCREENCAP_DIR names another folder, and
# the step number never repeats.
#
# The classified kind is printed for the saved folder too, using the same
# classifier the bot uses, so a capture says what Melisa would have called it:
#
#   scripts/screencap.sh sign-in
#   kind: sign_in
#
# Before committing a capture as a fixture, delete anything personal in it: a
# real code, an account name, a URL with a token.
set -euo pipefail

SOCKET="${SCREENCAP_SOCKET:-${QCAPTURE_SOCKET:-macbuilder}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${SCREENCAP_DIR:-$HERE/../logs/screencaps}"
HISTORY="${SCREENCAP_LINES:-${QCAPTURE_LINES:-120}}"
LABEL="${1:-step}"

# A label is a name, never a path: anything else becomes a dash.
SAFE_LABEL="$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9._-' '-')"
SAFE_LABEL="${SAFE_LABEL#-}"
[ -n "$SAFE_LABEL" ] || SAFE_LABEL="step"

if ! command -v tmux >/dev/null 2>&1; then
  echo "screencap: tmux is not installed (brew install tmux)" >&2
  exit 1
fi

SESSIONS="$(tmux -L "$SOCKET" list-sessions -F '#{session_name}' 2>/dev/null \
  | grep '^macbuilder-' || true)"
COUNT="$(printf '%s' "$SESSIONS" | grep -c '^macbuilder-' || true)"

if [ "$COUNT" -eq 0 ]; then
  echo "screencap: no macbuilder-* session on the '$SOCKET' socket" >&2
  exit 1
fi
if [ "$COUNT" -gt 1 ]; then
  echo "screencap: more than one session on the '$SOCKET' socket:" >&2
  while IFS= read -r name; do
    [ -n "$name" ] && printf '  %s\n' "$name" >&2
  done <<<"$SESSIONS"
  exit 1
fi
SESSION="$SESSIONS"

mkdir -p "$OUT_DIR"

STEP=1
for path in "$OUT_DIR"/[0-9][0-9]-*.txt; do
  [ -e "$path" ] || continue
  base="$(basename "$path")"
  number="${base%%-*}"
  case "$number" in
    '' | *[!0-9]*) continue ;;
  esac
  number=$((10#$number))
  if [ "$number" -ge "$STEP" ]; then
    STEP=$((number + 1))
  fi
done

STEM="$(printf '%02d-%s' "$STEP" "$SAFE_LABEL")"
PLAIN="$OUT_DIR/$STEM.txt"
COLOUR="$OUT_DIR/$STEM-color.txt"

tmux -L "$SOCKET" capture-pane -p -J -t "$SESSION" -S "-$HISTORY" >"$PLAIN"
tmux -L "$SOCKET" capture-pane -p -e -J -t "$SESSION" -S "-$HISTORY" >"$COLOUR"

printf '%s\n' "$PLAIN" "$COLOUR"

# The classifier is pure Python over the captured text: no tmux, no network.
if [ -x "$HERE/../.venv/bin/python" ]; then
  PYTHON="$HERE/../.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  PYTHON=""
fi
if [ -n "$PYTHON" ]; then
  "$PYTHON" - "$PLAIN" <<'PY' || echo "screencap: could not classify (screens.py needs Python 3.10+)"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screens  # noqa: E402

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
print("kind:", screens.classify(text).kind)
PY
fi
