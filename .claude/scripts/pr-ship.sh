#!/usr/bin/env bash
# The global `gh ship` alias runs <repo>/.claude/scripts/pr-ship.sh. agent-tools' canonical,
# generalized ship implementation lives at ci/ship/ship.sh — delegate to it so `gh ship`
# works in this repo with the same green-CI-gated merge + cleanup as everywhere else.
exec "$(git rev-parse --show-toplevel)/ci/ship/ship.sh" "$@"
