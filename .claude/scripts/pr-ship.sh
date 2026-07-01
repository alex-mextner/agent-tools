#!/usr/bin/env bash
# Provisioned by rig (ship_delegator). The global `gh ship` alias runs
# <repo>/.claude/scripts/pr-ship.sh. agent-tools' canonical, generalized ship
# implementation lives at ci/ship/ship.sh — delegate to it so `gh ship` works in this
# repo with the same green-CI-gated merge + cleanup as everywhere else. Repo-local
# ci/ship/ship.sh wins (agent-tools self-hosts); otherwise the rig-baked canonical path.
set -euo pipefail
toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"
repo_local="${toplevel:+$toplevel/ci/ship/ship.sh}"
if [[ -n "$repo_local" && -f "$repo_local" ]]; then
  exec "$repo_local" "$@"
fi
canonical=/Users/ultra/xp/agent-tools/ci/ship/ship.sh
if [[ -f "$canonical" ]]; then
  exec "$canonical" "$@"
fi
echo "pr-ship.sh: canonical ship.sh not found (repo-local $repo_local nor $canonical)." >&2
echo "Re-run 'rig apply' against a current agent-tools checkout to refresh it." >&2
exit 127
