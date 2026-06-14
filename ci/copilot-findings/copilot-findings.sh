#!/usr/bin/env bash
# Surface (and optionally gate on) GitHub Copilot's AI findings on a PR.
#
# IMPORTANT — there is NO single "Copilot findings" API. Copilot surfaces findings through
# TWO different, EXISTING GitHub surfaces, and this script reads both:
#
#   1) Copilot CODE REVIEW — when you request a review from Copilot (or auto-assign it), its
#      comments arrive as ordinary PR REVIEW COMMENTS authored by the bot
#      `copilot-pull-request-reviewer[bot]`. We list them via the pulls/{pr}/comments API
#      and filter by that author. (No special endpoint — they're normal review comments.)
#
#   2) Copilot AUTOFIX (code scanning) — when Copilot Autofix proposes fixes for code-
#      scanning alerts, those are CODE SCANNING ALERTS (requires GitHub Advanced Security /
#      a public repo). We read them via the code-scanning/alerts API, filtered to this PR's
#      ref. The "Copilot" part is the suggested autofix attached to each alert.
#
# So this is a SURFACING + optional gate tool over existing APIs — not a hidden feed.
#
# Requires: gh (authenticated). PR number from $1 or $PR_NUMBER.
#
# Knobs (env):
#   PR_NUMBER          the PR (or $1).
#   COPILOT_REVIEW_BOT review-comment author to treat as Copilot.
#                      Default: copilot-pull-request-reviewer[bot].
#   GATE               "1" = exit non-zero if any Copilot review comment OR open code-
#                      scanning alert on this PR is found. Default 0 (surface only — Copilot
#                      review is advisory, like any AI reviewer; see ci/ai-review/).
#
# Usage:
#   PR_NUMBER=123 sh ci/copilot-findings/copilot-findings.sh
#   GATE=1 sh ci/copilot-findings/copilot-findings.sh 123
set -euo pipefail

PR="${1:-${PR_NUMBER:-}}"
[ -n "$PR" ] || { echo "Usage: $0 <pr-number>  (or set PR_NUMBER)" >&2; exit 2; }
command -v gh >/dev/null 2>&1 || { echo "gh CLI not found" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq not found (needed to format findings) — install jq." >&2; exit 2; }
BOT="${COPILOT_REVIEW_BOT:-copilot-pull-request-reviewer[bot]}"
GATE="${GATE:-0}"

found=0

# --- (1) Copilot code-review comments (normal PR review comments by the bot) ------------
echo "== Copilot code-review comments (author: $BOT) =="
REVIEW_COMMENTS=$(gh api --paginate "repos/{owner}/{repo}/pulls/$PR/comments" \
  --jq "[.[] | select(.user.login==\"${BOT}\") | {path, line, body}]" 2>/dev/null || echo '[]')
RC_COUNT=$(printf '%s' "$REVIEW_COMMENTS" | jq 'length' 2>/dev/null || echo 0)
if [ "${RC_COUNT:-0}" -gt 0 ]; then
  found=$((found+RC_COUNT))
  printf '%s' "$REVIEW_COMMENTS" | jq -r '.[] | "  • \(.path):\(.line // "?") — \(.body | gsub("[\n\r]";" ") | .[0:160])"'
else
  echo "  (none — Copilot review not requested, or no comments)"
fi

# --- (2) Copilot Autofix / code-scanning alerts on this PR's ref (needs GHAS/public) -----
echo "== Open code-scanning alerts on this PR (Copilot Autofix attaches here) =="
HEAD_REF=$(gh pr view "$PR" --json headRefName -q '.headRefName' 2>/dev/null || echo "")
if [ -n "$HEAD_REF" ]; then
  ALERTS=$(gh api --paginate "repos/{owner}/{repo}/code-scanning/alerts?ref=refs/heads/${HEAD_REF}&state=open" \
    --jq "[.[] | {rule: .rule.id, sev: .rule.security_severity_level, path: .most_recent_instance.location.path, line: .most_recent_instance.location.start_line}]" 2>/dev/null || echo '__ERR__')
  if [ "$ALERTS" = "__ERR__" ]; then
    echo "  (code-scanning API unavailable — needs GitHub Advanced Security on private repos, or a public repo)"
  else
    AC=$(printf '%s' "$ALERTS" | jq 'length' 2>/dev/null || echo 0)
    if [ "${AC:-0}" -gt 0 ]; then
      found=$((found+AC))
      printf '%s' "$ALERTS" | jq -r '.[] | "  • [\(.sev // "?")] \(.rule) — \(.path):\(.line // "?")"'
    else
      echo "  (no open code-scanning alerts on this ref)"
    fi
  fi
else
  echo "  (could not resolve PR head ref)"
fi

echo "== total Copilot-surfaced findings: $found =="
if [ "$GATE" = "1" ] && [ "$found" -gt 0 ]; then
  echo "::error::GATE=1 and $found Copilot finding(s) present — address them or set GATE=0 (advisory)." >&2
  exit 1
fi
exit 0
