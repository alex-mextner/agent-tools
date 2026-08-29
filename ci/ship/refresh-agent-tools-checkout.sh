#!/usr/bin/env bash
# refresh-agent-tools-checkout.sh — periodic self-heal for the AGENT_TOOLS_ROOT checkout.
#
# Problem this solves (agent-tools#315, the ci/ship-specific slice of it): the live
# AGENT_TOOLS_ROOT checkout that `gh ship` execs straight off disk (see
# ~/.config/agent-tools/env + .claude/scripts/pr-ship.sh in every managed repo) has no
# mechanism that keeps it synced with origin/main. A merged fix silently does not take
# effect on a machine until someone remembers to `git pull` that checkout by hand. The
# ci/ship/ship.sh "agent-tools checkout staleness gate" catches this AT SHIP TIME (refuses
# with a clear fix command when ci/ship/ itself is stale) — this script is the general,
# proactive counterpart: run it on a timer (launchd/cron) so the checkout self-heals BEFORE
# anyone hits the gate at all.
#
# Deliberately conservative — a `git merge --ff-only` only ever fast-forwards, so it can
# never rewrite history, but this script goes further and refuses to touch the checkout
# unless ALL of these hold:
#   1. currently on `main`             (never switches branches, never touches a feature
#                                        checkout mid-development — see the incident this
#                                        was written after: this exact checkout was once
#                                        found parked on a feature branch with real WIP)
#   2. working tree is clean           (no uncommitted TRACKED changes to clobber or strand)
#   3. no commits ahead of @{upstream} (nothing unpushed that a pull could complicate)
#   4. no path origin/main is about to change collides with a currently-IGNORED file on
#      disk (guard 2's `git status --porcelain` does NOT list ignored paths at all, and
#      git's own untracked-file overwrite protection does NOT cover ignored files either —
#      verified empirically: a plain `git pull --ff-only` silently overwrites an ignored
#      file the incoming commit newly tracks. Guard 2 alone does not catch this.)
#   5. no `ci/ship/ship.sh` from this checkout currently appears to be running (best-effort
#      `pgrep`; avoids the narrow window where an unattended pull rewrites the very script
#      file a live `gh ship` invocation is mid-way through interpreting)
# Any guard failing exits 0 SILENTLY — a periodic job that logs noise on every ordinary
# "someone is actively working in here" state gets ignored (or worse, muted) by whoever
# reads the log, defeating its own purpose on the one occasion it matters. A fast-forward
# that actually happens DOES log one line (old SHA -> new SHA) — signal, not noise.
#
# Usage: refresh-agent-tools-checkout.sh [checkout-path]
#   With NO argument: checkout-path is $AGENT_TOOLS_ROOT, else the directory two levels up
#   from this script's own location (mirrors ship.sh's BASH_SOURCE-derived self-location).
#   With an EXPLICIT argument that doesn't exist: exits 1 (a typo'd path must be loud, not
#   silently redirected to the self-location fallback — that fallback exists only for the
#   convenience of the no-argument launchd/cron invocation).
#
# Install (macOS, launchd — mirrors the existing ai.hyperide.model-freshness.plist pattern,
# see lib/checker/model_freshness.py and issue #186). NOTE the explicit PATH: launchd runs a
# LaunchAgent with a minimal environment (/usr/bin:/bin:/usr/sbin:/sbin), not your shell's —
# without a PATH entry a Homebrew git/gtimeout would not resolve.
#   Write ~/Library/LaunchAgents/ai.hyperide.agent-tools-refresh.plist:
#     <?xml version="1.0" encoding="UTF-8"?>
#     <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#     <plist version="1.0"><dict>
#       <key>Label</key><string>ai.hyperide.agent-tools-refresh</string>
#       <key>ProgramArguments</key><array>
#         <string>/bin/bash</string>
#         <string>/Users/YOU/xp/agent-tools/ci/ship/refresh-agent-tools-checkout.sh</string>
#       </array>
#       <key>EnvironmentVariables</key><dict>
#         <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
#       </dict>
#       <key>StartInterval</key><integer>1800</integer>  <!-- every 30 min -->
#       <key>RunAtLoad</key><false/>
#       <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/ai.hyperide.agent-tools-refresh.log</string>
#       <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/ai.hyperide.agent-tools-refresh.log</string>
#     </dict></plist>
#   Then: launchctl load ~/Library/LaunchAgents/ai.hyperide.agent-tools-refresh.plist
# Linux (cron): */30 * * * * /bin/bash /path/to/agent-tools/ci/ship/refresh-agent-tools-checkout.sh
#
# Known, accepted residual limitation (best-effort posture, not a security boundary): guard 4
# re-validates collisions once, immediately before the fetch, but does not hold a lock across
# the fetch+merge — a file created at a colliding path in that narrow window could still be
# overwritten. Given this script's own conservative "do nothing unless clearly safe" design
# and the millisecond-scale window, a full cross-process lock was judged not worth the added
# complexity; revisit if this ever proves wrong in practice.
set -euo pipefail

_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || echo /nonexistent)"

if [ $# -ge 1 ]; then
  [ -d "$1" ] || { echo "refresh-agent-tools-checkout.sh: '$1' is not a directory." >&2; exit 1; }
  # Canonicalize even an explicit argument: an unresolved relative/symlinked path would make
  # guard 5's pgrep pattern (built from this value) never match a real absolute cmdline.
  ROOT="$(cd "$1" 2>/dev/null && pwd -P || true)"
else
  ROOT="${AGENT_TOOLS_ROOT:-}"
  [ -n "$ROOT" ] && [ -d "$ROOT" ] && ROOT="$(cd "$ROOT" 2>/dev/null && pwd -P || true)"
  [ -n "$ROOT" ] || ROOT="$(cd "$_SELF_DIR/../.." 2>/dev/null && pwd -P || true)"
fi

# timeout(1) is GNU coreutils, absent from a stock macOS PATH (and from a launchd
# LaunchAgent's minimal PATH even when Homebrew has it as `gtimeout`, unless the plist's own
# PATH names its bin dir — see the install recipe above). Resolve once; fall back to running
# unwrapped (still HTTP-speed-bounded via GIT_HTTP_LOW_SPEED_* below) rather than silently
# no-op'ing the one thing this script exists to do.
_TIMEOUT_BIN="$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true)"
_run_timeout() {  # $1 = seconds, rest = command
  local secs="$1"; shift
  if [ -n "$_TIMEOUT_BIN" ]; then "$_TIMEOUT_BIN" "$secs" "$@"; else "$@"; fi
}

_is_git_worktree() {
  # NOT `-d "$1/.git"` — a LINKED worktree's .git is a FILE, not a directory.
  [ "$(git -C "$1" rev-parse --is-inside-work-tree 2>/dev/null || true)" = "true" ]
}

_guards_ok() {
  local root="$1"
  _is_git_worktree "$root" || return 1
  [ "$(git -C "$root" symbolic-ref --quiet --short HEAD 2>/dev/null || true)" = "main" ] || return 1
  [ -z "$(git -C "$root" status --porcelain 2>/dev/null)" ] || return 1
  local ahead
  ahead=$(git -C "$root" rev-list --count '@{u}..HEAD' 2>/dev/null) || return 1
  [ "$ahead" = "0" ] || return 1
  return 0
}

# Guard 4: an ignored (hence invisible to `status --porcelain`) file on disk whose path is
# about to start being tracked by origin/main would be silently overwritten by --ff-only —
# git's untracked-file overwrite protection does not extend to ignored paths. Only refuses
# on an ACTUAL path collision (not "any ignored file exists anywhere" — that would refuse on
# nearly every real checkout, given __pycache__/.worktrees/build output, defeating the
# script). Requires origin/main to already be fetched.
#
# NUL-delimited throughout (`-z`, `core.quotePath=false`) — plain `--name-only` C-quotes any
# path with non-ASCII bytes or embedded quotes/newlines (verified: default
# core.quotePath=true), which would make `[ -e "$root/$p" ]` test a pathname that never
# exists on disk and silently miss the exact collision this guard exists to catch. `-e` alone
# also misses a DANGLING ignored symlink (false for a broken link) at a path origin/main is
# about to start tracking — checked via `-e || -L` instead.
#
# Builds both path lists with ONE `git` spawn each (not one `check-ignore` spawn per changed
# path — a checkout behind a large merge can have hundreds of changed paths); portable to
# bash 3.2 (no associative arrays / readarray), which matters because macOS ships bash 3.2 as
# /bin/bash.
#
# A collision is not just PATH EQUALITY — an ancestor/descendant relationship also collides
# (review-cli finding, GH-470, reproduced): an incoming tracked file `cache` replacing an
# ignored DIRECTORY containing `cache/x` (`cache` is a prefix of the ignored path) is a real
# collision even though "cache" != "cache/x" — a plain equality check misses it and --ff-only
# silently deletes `cache/x` to make room. Same the other direction: an incoming tracked path
# `foo/bar` where `foo` itself is currently an ignored file/symlink (`foo` is a prefix of the
# changed path) also collides — git must clobber the ignored `foo` to create the `foo/`
# directory the merge needs. So each changed path is checked against every ignored path for
# equality OR either being a path-prefix of the other, not `_in_array` exact membership alone.
_ignored_collision() {
  local root="$1" p ig
  local -a changed=() ignored=()
  while IFS= read -r -d '' p; do changed+=("$p"); done \
    < <(git -C "$root" -c core.quotePath=false diff --name-only -z HEAD..origin/main 2>/dev/null)
  [ "${#changed[@]}" -gt 0 ] || return 0
  while IFS= read -r -d '' p; do ignored+=("$p"); done \
    < <(git -C "$root" -c core.quotePath=false ls-files -oi --exclude-standard -z 2>/dev/null)
  [ "${#ignored[@]}" -gt 0 ] || return 0
  for p in "${changed[@]}"; do
    for ig in "${ignored[@]}"; do
      # Exact match, or the changed path is an ancestor of the ignored path (a directory
      # about to be replaced) — anchor the existence check on the CHANGED path (that's what's
      # on disk today as the thing about to be overwritten).
      case "$ig" in
        "$p"|"$p"/*)
          if { [ -e "$root/$p" ] || [ -L "$root/$p" ]; }; then
            printf '%s\n' "$p"
            return 0
          fi
          ;;
      esac
      # The changed path is a descendant of the ignored path (an ignored file/symlink about
      # to be replaced by a directory) — anchor the existence check on the IGNORED path.
      case "$p" in
        "$ig"/*)
          if { [ -e "$root/$ig" ] || [ -L "$root/$ig" ]; }; then
            printf '%s\n' "$p"
            return 0
          fi
          ;;
      esac
    done
  done
  return 0
}

# Guard 5: best-effort — skip if a ship.sh invocation from THIS checkout looks to be running,
# so an unattended pull can't rewrite the script file a live `gh ship` is mid-way through
# interpreting. Detection only, never signals/kills anything (matches this ecosystem's
# pkill-guard posture: observe, don't touch another process). ROOT is already canonicalized
# above, so the pattern always matches a real absolute cmdline; ERE metacharacters in ROOT
# (rare, but possible in a path) degrade toward a false-positive SKIP — i.e. the safe
# direction for a best-effort guard — never toward silently missing a live process.
_ship_running() {
  local escaped
  escaped=$(printf '%s' "${1%/}/ci/ship/ship.sh" | sed 's/[.[\*^$()+?{|]/\\&/g')
  pgrep -f "$escaped" >/dev/null 2>&1
}

[ -n "$ROOT" ] || exit 0
_guards_ok "$ROOT" || exit 0
_ship_running "$ROOT" && exit 0

GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=10 \
  _run_timeout 30 git -C "$ROOT" fetch -q origin main 2>/dev/null || exit 0

_OLD_SHA="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
_NEW_SHA="$(git -C "$ROOT" rev-parse origin/main 2>/dev/null || true)"
[ -n "$_OLD_SHA" ] && [ -n "$_NEW_SHA" ] && [ "$_OLD_SHA" != "$_NEW_SHA" ] || exit 0

# `|| exit 0`: the "any guard failing exits 0 silently" contract must hold even if the
# substitution itself fails (a transient git error inside _ignored_collision), not just when
# it succeeds with a non-empty result — a bare unprotected assignment under `set -e` would
# otherwise let a rare internal failure kill the script with a non-zero, log-visible exit.
_COLLISION="$(_ignored_collision "$ROOT")" || exit 0
[ -z "$_COLLISION" ] || exit 0

# --ff-only, already fetched above — `merge` (not `pull`) avoids a second network round-trip
# and keeps the fetch/merge decision points explicit and independently testable.
if git -C "$ROOT" merge --ff-only --quiet origin/main 2>/dev/null; then
  echo "[refresh-agent-tools-checkout] $ROOT: ${_OLD_SHA:0:12} -> ${_NEW_SHA:0:12}"
fi
