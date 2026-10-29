#!/bin/bash
# /status: branch, git state and worktrees. The bot adds queue information.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

cd "$REPO_DIR"
echo "path: $REPO_DIR"
echo "branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "head: $(git log -1 --format='%h %s' 2>/dev/null || echo none)"

if upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
  counts="$(git rev-list --left-right --count "$upstream"...HEAD 2>/dev/null || echo '? ?')"
  echo "upstream: $upstream (behind/ahead: $counts)"
else
  echo "upstream: none"
fi

echo "dirty: $(git status --porcelain | wc -l | tr -d ' ') file(s)"
echo "worktrees:"
git worktree list --porcelain | sed -n 's/^worktree /  /p'
echo "--- git status ---"
git status --short --branch | head -n 40
