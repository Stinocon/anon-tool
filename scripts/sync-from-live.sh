#!/usr/bin/env bash
#
# sync-from-live.sh — mirror the anon-tool CODE from its live runtime home into this repository.
#
# Direction:  live (${ANON_HOME:-~/.anon})  ->  repo.
# The live directory is what actually runs (the Pi guard spawns ~/.anon/anon.py, the skill and
# the DECs point at it), so it stays the runtime source of truth; this repo is the versioned
# copy. Same shape as pi-customization's sync-to-repo.sh, with a different data policy:
#
#   mirrored  : anon.py, deanon.py, tests/, catalogs/, web/, docs/DESIGN.md
#   repo-only : README.md, LICENSE, Dockerfile, docker-compose.yml, scripts/, .gitignore
#   NEVER     : maps/, entities.txt, allow.txt, *.map.json, *.redacted.*, __pycache__
#
# The deterministic gate runs before the commit: a commit with a red test suite is refused.
#
# Usage: bash scripts/sync-from-live.sh          (PI_ANON_SYNC_PUSH=0 to skip the push)
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE="${ANON_HOME:-$HOME/.anon}"
[ -d "$LIVE" ] || { echo "[anon-sync] live dir not found: $LIVE" >&2; exit 0; }

BRANCH="${PI_ANON_SYNC_BRANCH:-main}"
REMOTE="${PI_ANON_SYNC_REMOTE:-origin}"

# --- lock (one sync at a time) -------------------------------------------
LOCK="${TMPDIR:-/tmp}/anon-tool-sync.lock"
while ! mkdir "$LOCK" 2>/dev/null; do sleep 0.2; done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# --- mirror the code subset ----------------------------------------------
for f in anon.py deanon.py convert.py; do
  [ -f "$LIVE/$f" ] && cp "$LIVE/$f" "$REPO/$f"
done
for d in tests catalogs web; do
  [ -d "$LIVE/$d" ] && rsync -a --delete "$LIVE/$d/" "$REPO/$d/"
done

# --- deterministic gate: refuse to commit a red suite --------------------
if [ -f "$REPO/tests/ui_load_check.mjs" ]; then
  if ! node "$REPO/tests/ui_load_check.mjs" >/tmp/anon-sync-ui.log 2>&1; then
    echo "[anon-sync] UI LOAD FAILURE (app.js throws at load) — not committing:" >&2
    tail -10 /tmp/anon-sync-ui.log >&2
    exit 1
  fi
fi

for suite in tests/test_anon.py tests/test_web.py; do
  [ -f "$REPO/$suite" ] || continue
  if ! python3 "$REPO/$suite" >/tmp/anon-sync-tests.log 2>&1; then
    echo "[anon-sync] TEST FAILURE in $suite — not committing. Output:" >&2
    tail -20 /tmp/anon-sync-tests.log >&2
    exit 1
  fi
done

# --- commit + push if anything changed -----------------------------------
cd "$REPO" || exit 0
git add -A
if git diff --cached --quiet; then
  echo "[anon-sync] nothing to commit"
  exit 0
fi
git commit -q -m "chore(sync): mirror anon engine from ${LIVE/#$HOME/~}" \
  --author="anon-tool sync <anon-sync@localhost>"
if [ "${PI_ANON_SYNC_PUSH:-1}" = "1" ]; then
  git push -q "$REMOTE" "$BRANCH" 2>/dev/null || echo "[anon-sync] committed locally; push skipped/failed" >&2
else
  echo "[anon-sync] committed locally (push disabled)"
fi
