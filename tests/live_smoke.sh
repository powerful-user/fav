#!/usr/bin/env bash
# Manual check against the real X API. It spends real credits.
set -euo pipefail
if [ "$(printenv FAV_LIVE || true)" != "1" ]; then
  echo "live smoke skipped (set FAV_LIVE=1 to run it)"
  exit 0
fi
root="$(cd "$(dirname "$0")/.." && pwd)"
fav() { PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" python3 -B -m fav "$@"; }
step() { printf '\n== %s\n' "$*"; }
step "auth status"
fav auth status
step "search"
found="$(fav search "agent-first programming languages" --limit 10 --json)"
printf '%s\n' "$found"
step "post"
first="$(printf '%s' "$found" | python3 -c 'import json, sys; items = json.load(sys.stdin)["items"]; print(items[0]["id"] if items else "")')"
if [ -n "$first" ]; then
  fav post "$first"
else
  echo "search found no recent posts; post skipped"
fi
step "bookmarks sync"
fav bookmarks sync
step "bookmarks search"
fav bookmarks search agent --limit 5
step "usage"
fav usage
echo
echo "live smoke done: compare the ledger estimate above with the change in the X credit balance"
