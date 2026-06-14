#!/usr/bin/env bash
# AI code review on a diff — generic runner. Wraps a CONFIGURABLE review CLI (codex,
# review-cli, claude, gemini, opencode, …), feeds it the PR / branch diff, and prints the
# findings. Usable in CI (see ai-review.yml) OR locally before you commit/ship.
#
# This is vendor-neutral: it does NOT hard-code any model or provider. You point
# AI_REVIEW_CMD at whatever review tool you have, supplying its API key via the env/secret
# that tool expects. Two built-in modes cover the common CLIs; otherwise pass a full custom
# command.
#
# Knobs (env):
#   AI_REVIEW_CMD    Full command to run. The diff is passed two SAFE ways:
#                      1) if the command contains the token {DIFF_FILE}, it's replaced with
#                         a path to a temp file holding the diff;
#                      2) else the diff is piped to the command on STDIN.
#                    The diff TEXT is never inlined into the command — it's attacker-
#                    controlled (the PR's content), so inlining it into an `eval` would be a
#                    command-injection / secret-exfiltration hole. Use {DIFF_FILE} or STDIN.
#                    If unset, AI_REVIEW_TOOL (below) selects a sensible default.
#   AI_REVIEW_TOOL   Shorthand when AI_REVIEW_CMD is unset. Built-in:
#                      codex  -> pipes the computed diff to `codex exec "<review prompt>"`
#                    For any other reviewer, set AI_REVIEW_CMD (the diff comes in via
#                    {DIFF_FILE} or STDIN). Default: codex.
#   AI_REVIEW_BASE   Base ref for the diff range. Default: origin/main (falls back to main).
#   AI_REVIEW_HEAD   Head ref. Default: HEAD.
#   AI_REVIEW_OUT    Write findings to this file (also echoed to stdout). Default: stdout only.
#   AI_REVIEW_FAIL   "1" = exit non-zero if the tool itself errors (NOT on findings — AI
#                    review is advisory, it should not hard-block a merge). Default: 0.
#
# The built-in defaults review the DIFF THIS SCRIPT COMPUTES (base...head) — so they work in
# CI where the PR is already committed. For LOCAL review of UNCOMMITTED work, set e.g.
# AI_REVIEW_CMD='codex exec review --uncommitted'.
#
# Usage:
#   sh ci/ai-review/ai-review.sh                          # codex reviews the PR diff
#   AI_REVIEW_CMD='my-reviewer --diff {DIFF_FILE}' sh ci/ai-review/ai-review.sh
set -euo pipefail

AI_REVIEW_TOOL="${AI_REVIEW_TOOL:-codex}"
AI_REVIEW_BASE="${AI_REVIEW_BASE:-origin/main}"
AI_REVIEW_HEAD="${AI_REVIEW_HEAD:-HEAD}"
AI_REVIEW_OUT="${AI_REVIEW_OUT:-}"
AI_REVIEW_FAIL="${AI_REVIEW_FAIL:-0}"

# Resolve a usable base ref (origin/main -> main -> empty = whole working tree).
resolve_base() {
  if git rev-parse --verify --quiet "$AI_REVIEW_BASE" >/dev/null 2>&1; then
    printf '%s' "$AI_REVIEW_BASE"; return
  fi
  if git rev-parse --verify --quiet main >/dev/null 2>&1; then
    printf '%s' "main"; return
  fi
  printf ''  # no base — tools that review the working tree handle this
}

BASE="$(resolve_base)"

DIFF_FILE="$(mktemp -t ai-review-diff.XXXXXX)"
trap 'rm -f "$DIFF_FILE"' EXIT

if [ -n "$BASE" ]; then
  git diff "$BASE...$AI_REVIEW_HEAD" > "$DIFF_FILE" 2>/dev/null || git diff "$BASE" "$AI_REVIEW_HEAD" > "$DIFF_FILE" || true
else
  git diff > "$DIFF_FILE" || true
fi

if [ ! -s "$DIFF_FILE" ]; then
  echo "[ai-review] empty diff (base='$BASE', head='$AI_REVIEW_HEAD') — nothing to review." >&2
  exit 0
fi

# Pick the command. The built-in defaults review the DIFF THIS SCRIPT COMPUTED (read from
# STDIN / a file), NOT the working tree — so they work in CI where the PR is already
# committed (a `--uncommitted` review would see nothing). For LOCAL pre-commit review of
# UNCOMMITTED changes instead, set AI_REVIEW_CMD explicitly, e.g.
#   AI_REVIEW_CMD='codex exec review --uncommitted'
CMD="${AI_REVIEW_CMD:-}"
if [ -z "$CMD" ]; then
  case "$AI_REVIEW_TOOL" in
    # Pipe the computed diff in (STDIN, below) and ask the model to review it. codex reads
    # the piped diff as context — so this reviews the actual PR diff, in CI, correctly.
    codex)  CMD='codex exec "Review the code diff on stdin for bugs, security issues, and quality. Be concise; cite file:line."' ;;
    *) echo "[ai-review] AI_REVIEW_TOOL='$AI_REVIEW_TOOL' has no built-in default — set AI_REVIEW_CMD to your reviewer's command (it receives the diff via {DIFF_FILE} or STDIN). E.g. AI_REVIEW_CMD='review --diff {DIFF_FILE}'." >&2; exit 2 ;;
  esac
fi

run_review() {
  # Pass the diff via a FILE PATH ({DIFF_FILE}) or on STDIN — never by inlining the diff
  # TEXT into the command. The diff is attacker-controlled (it's the PR's content); inlining
  # it into an `eval` would be a command-injection -> secret-exfiltration hole (the CI job
  # holds the review API key). So only the path token and stdin are supported, and the file
  # path is a fixed mktemp name with no shell metacharacters.
  if printf '%s' "$CMD" | grep -q '{DIFF_FILE}'; then
    eval "${CMD//\{DIFF_FILE\}/$DIFF_FILE}"
  else
    eval "$CMD" < "$DIFF_FILE"
  fi
}

echo "[ai-review] running: $CMD" >&2
set +e
OUTPUT="$(run_review 2>&1)"
RC=$?
set -e

printf '%s\n' "$OUTPUT"
if [ -n "$AI_REVIEW_OUT" ]; then
  printf '%s\n' "$OUTPUT" > "$AI_REVIEW_OUT"
fi

if [ "$RC" -ne 0 ]; then
  echo "[ai-review] tool exited $RC." >&2
  [ "$AI_REVIEW_FAIL" = "1" ] && exit "$RC"
fi
exit 0
