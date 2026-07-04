#!/usr/bin/env bash
# Provisioned by rig (ship_delegator). The global `gh ship` alias runs
# <repo>/.claude/scripts/pr-ship.sh. agent-tools' canonical, generalized ship
# implementation lives at ci/ship/ship.sh — delegate to it so `gh ship` works in this
# repo with the same green-CI-gated merge + cleanup as everywhere else. Repo-local
# ci/ship/ship.sh wins (agent-tools self-hosts); otherwise $AGENT_TOOLS_ROOT — from the
# environment, or from the machine-level env file rig apply writes. This script is
# PORTABLE (no machine-specific paths), so a repo may commit it verbatim.
set -euo pipefail
toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"
repo_local="${toplevel:+$toplevel/ci/ship/ship.sh}"
if [[ -n "$repo_local" && -f "$repo_local" ]]; then
  exec "$repo_local" "$@"
fi
env_file="${XDG_CONFIG_HOME:-${HOME:-}/.config}/agent-tools/env"
if [[ -z "${AGENT_TOOLS_ROOT:-}" && ! -L "$env_file" && -f "$env_file" ]]; then
  # shellcheck source=/dev/null
  source "$env_file"
fi
canonical="${AGENT_TOOLS_ROOT:+$AGENT_TOOLS_ROOT/ci/ship/ship.sh}"
if [[ -n "$canonical" && -f "$canonical" ]]; then
  export AGENT_TOOLS_ROOT
  exec "$canonical" "$@"
fi
echo "pr-ship.sh: canonical ship.sh not found (repo-local $repo_local; AGENT_TOOLS_ROOT=${AGENT_TOOLS_ROOT:-<unset>}; env file $env_file)." >&2
echo "Set AGENT_TOOLS_ROOT (or write $env_file), or re-run 'rig apply'." >&2
exit 127
