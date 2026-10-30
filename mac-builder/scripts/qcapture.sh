#!/bin/bash
# Snapshot a live question box: the plain pane and the coloured pane.
#
#   scripts/qcapture.sh                 -> 01-step.txt      01-step-color.txt
#   scripts/qcapture.sh after-down      -> 02-after-down.txt 02-after-down-color.txt
#
# The macbuilder-* tmux session is found on the macbuilder socket and read with
# capture-pane only: nothing is typed into it, so a capture can never disturb a
# job that is waiting for an answer. Files go to ~/Desktop/qbox/ (QBOX_DIR
# overrides that) and the step number never repeats.
set -euo pipefail

SOCKET="${QCAPTURE_SOCKET:-macbuilder}"
OUT_DIR="${QBOX_DIR:-$HOME/Desktop/qbox}"
HISTORY="${QCAPTURE_LINES:-120}"
LABEL="${1:-step}"

# A label is a name, never a path: anything else becomes a dash.
SAFE_LABEL="$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9._-' '-')"
SAFE_LABEL="${SAFE_LABEL#-}"
[ -n "$SAFE_LABEL" ] || SAFE_LABEL="step"

if ! command -v tmux >/dev/null 2>&1; then
  echo "qcapture: tmux is not installed (brew install tmux)" >&2
  exit 1
fi

SESSIONS="$(tmux -L "$SOCKET" list-sessions -F '#{session_name}' 2>/dev/null \
  | grep '^macbuilder-' || true)"
COUNT="$(printf '%s' "$SESSIONS" | grep -c '^macbuilder-' || true)"

if [ "$COUNT" -eq 0 ]; then
  echo "qcapture: no macbuilder-* session on the '$SOCKET' socket" >&2
  exit 1
fi
if [ "$COUNT" -gt 1 ]; then
  echo "qcapture: more than one session on the '$SOCKET' socket:" >&2
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

tmux -L "$SOCKET" capture-pane -p -t "$SESSION" -S "-$HISTORY" >"$PLAIN"
tmux -L "$SOCKET" capture-pane -p -e -t "$SESSION" -S "-$HISTORY" >"$COLOUR"

printf '%s\n' "$PLAIN" "$COLOUR"
