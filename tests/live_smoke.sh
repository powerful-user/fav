#!/usr/bin/env bash
# Manual Phase 1 check against the real X API. It spends real credits.
set -euo pipefail
if [ "$(printenv WINGS_X_LIVE || true)" != "1" ]; then
  echo "live smoke skipped (set WINGS_X_LIVE=1 to run it)"
  exit 0
fi
root="$(cd "$(dirname "$0")/.." && pwd)"
wx="$root/bin/wings-x"
step() { printf '\n== %s\n' "$*"; }
step "auth status"
"$wx" auth status
step "search"
found="$("$wx" search "agent-first programming languages" --limit 10 --json)"
printf '%s\n' "$found"
step "post"
first="$(printf '%s' "$found" | python3 -c 'import json, sys; items = json.load(sys.stdin)["items"]; print(items[0]["id"] if items else "")')"
if [ -n "$first" ]; then
  "$wx" post "$first"
else
  echo "search found no recent posts; post skipped"
fi
step "bookmarks sync"
"$wx" bookmarks sync
step "bookmarks search"
"$wx" bookmarks search agent --limit 5
step "usage"
"$wx" usage
echo
echo "live smoke done: compare the ledger estimate above with the change in the X credit balance"
