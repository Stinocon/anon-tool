#!/usr/bin/env bash
#
# install-git-hooks.sh — copy this repository's git hooks into its .git/hooks/.
#
# Git hooks are not versioned, so the checkable copy lives in `git-hooks/` and is installed here.
# Installing per repository (not through a global `core.hooksPath`) is deliberate: a global hooks
# path would silently disable every repository's own hooks, which is worse than the problem it solves.
#
#   bash scripts/install-git-hooks.sh
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$REPO/git-hooks"

if [ ! -d "$SOURCE" ]; then
  echo "install-git-hooks: no git-hooks/ directory in $REPO — nothing to install"
  exit 0
fi

GIT_DIR="$(git -C "$REPO" rev-parse --absolute-git-dir 2>/dev/null)" || {
  echo "install-git-hooks: $REPO is not a git repository" >&2
  exit 2
}
DEST="$GIT_DIR/hooks"
mkdir -p "$DEST"

# A global hooksPath makes .git/hooks/ dead. An installed hook that git never runs is the worst
# outcome — it looks like protection and is not — so this is a REFUSAL, not a warning.
CONFIGURED="$(git -C "$REPO" config --get core.hooksPath || true)"
if [ -n "$CONFIGURED" ]; then
  echo "install-git-hooks: REFUSING — core.hooksPath=$CONFIGURED is set, so .git/hooks/ is not consulted" >&2
  echo "  The hook would look installed and never run. Unset it (git config --unset core.hooksPath)," >&2
  echo "  or install the hooks into $CONFIGURED yourself." >&2
  exit 1
fi

shopt -s nullglob
for hook in "$SOURCE"/*; do
  name="$(basename "$hook")"
  cp "$hook" "$DEST/$name"
  chmod +x "$DEST/$name"
  echo "install-git-hooks: installed $name -> $DEST/$name"
done
